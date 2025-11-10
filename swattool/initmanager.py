#!/usr/bin/env python3

"""Management of swattool initialization.

This module handles the preparation of build instances on swattool startup. It
manages data fetching from remote sources, SQLite database storage, and
instantiation of data classes. The implementation uses a phased approach with
concurrent execution to improve performance.
"""

import concurrent.futures
import enum
import json
import logging
import os
import sqlite3
from typing import Any, Collection
from typing import Callable, Optional

import pygit2  # type: ignore
from tqdm.contrib.logging import logging_redirect_tqdm
import tqdm

from .bugzilla import Bugzilla
from . import buildbotrest
from . import database
from . import logfingerprint
from . import pokyciarchive
from . import swatbotrest
from . import swatbuild
from . import swatlogs
from . import userdata
from . import utils

logger = logging.getLogger(__name__)


def _ab_url_is_valid(url: str) -> bool:
    return '.yoctoproject.org' in url


class InitPhase(enum.IntEnum):
    """An initialization phase.

    Represents distinct phases of the initialization process, allowing for
    better progress tracking and dependency management between tasks.
    """

    WARM_UP = enum.auto()
    FAILURES_LIST = enum.auto()
    FAILURES_DATA = enum.auto()
    COLLECTIONS_DATA = enum.auto()
    LOGS = enum.auto()
    AB_INTS = enum.auto()
    POKY_CI_ARCHIVE = enum.auto()
    DONE = enum.auto()

    def __str__(self) -> str:
        descriptions = {
            InitPhase.WARM_UP: 'Initialization',
            InitPhase.FAILURES_LIST: 'Fetching failures list',
            InitPhase.FAILURES_DATA: 'Fetching failures data',
            InitPhase.COLLECTIONS_DATA: 'Fetching collections data',
            InitPhase.LOGS: 'Fetching and preparing logs',
            InitPhase.AB_INTS: 'Fetching AB-INT list',
            InitPhase.POKY_CI_ARCHIVE: 'Fetching poky-ci-archive git',
        }
        return descriptions.get(self, self.name)


class InitExecutor:
    """A thread pool executor with progress bar.

    Manages concurrent execution of initialization jobs with a visual progress
    indicator.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, for_review: bool):
        cpus = os.cpu_count()
        threads = min(16, cpus) if cpus else 16
        self._executor = concurrent.futures.ThreadPoolExecutor(threads)
        self._jobs: dict[InitPhase, set[concurrent.futures.Future]] = {}
        self._jobs_done: dict[InitPhase, list[concurrent.futures.Future]] = {}
        self.done = False
        self.stopping = False

        self.phase_weight = {
            InitPhase.WARM_UP: 1,
            InitPhase.FAILURES_LIST: 100,
            InitPhase.FAILURES_DATA: 200,
            InitPhase.COLLECTIONS_DATA: 100,
            InitPhase.LOGS: 500 if for_review else 0,
            InitPhase.AB_INTS: 10 if for_review else 0,
            InitPhase.POKY_CI_ARCHIVE: 200 if for_review else 0,
            InitPhase.DONE: 1,
        }

        bar_format = "{l_bar}{bar}| [{elapsed}<{remaining}{postfix}]"
        self.progress_bar = tqdm.tqdm(
            total=sum(self.phase_weight.values()),
            desc="Loading failures",
            bar_format=bar_format
        )
        self.progress = 0

        self._update_progress(InitPhase.WARM_UP)
        self.progress_bar.refresh()

    def submit(self, phase: InitPhase, fn, *args, **kwargs) -> None:
        """Submit a new job to the executor.

        Args:
            phase: The initialization phase this job belongs to
            fn: Function to execute
            *args: Positional arguments for the job function
            **kwargs: Keyword arguments for the job function
        """
        if not self.stopping:
            job = self._executor.submit(fn, *args, **kwargs)
            self._jobs.setdefault(phase, set()).add(job)

    def wait_phase_done(self, phase: InitPhase) -> None:
        """Wait until all tasks of a specific phase of init are done."""
        self._run(lambda: all(p > phase or len(j) == 0
                              for p, j in self._jobs.items()))

    def wait_all(self) -> None:
        """Wait until all tasks are done."""
        self._run(lambda: sum(len(j) for j in self._jobs.values()) == 0)
        if not self.stopping:
            assert sum(len(j) for j in self._jobs.values()) == 0
            self.done = True

        self.progress_bar.close()

    def _wait_next_done(self, phase: InitPhase) -> None:
        wait_return = concurrent.futures.FIRST_COMPLETED
        done, _ = concurrent.futures.wait(self._jobs[phase],
                                          return_when=wait_return)
        if self.stopping:
            return
        for fut in done:
            err = fut.exception()
            if (err and isinstance(err, utils.SwattoolException)
                    and not isinstance(err, utils.LoginRequiredException)):
                if phase == InitPhase.FAILURES_LIST:
                    errstr = f"Failed to fetch failures list: {str(err)}"
                    raise utils.SwattoolException(errstr) from err
                logging.warning(str(err))
            elif err:
                raise err
            else:
                res = fut.result()
                if res:
                    fn, *args = res
                    fn(*args)
            self._jobs[phase].remove(fut)
            self._jobs_done.setdefault(phase, []).append(fut)

    def _run(self, end_cond: Callable) -> None:
        try:
            for phase in InitPhase:
                while self._jobs.get(phase) and not (end_cond()
                                                     or self.stopping):
                    self._update_progress(phase)
                    self._wait_next_done(phase)
        except KeyboardInterrupt:
            self.stopping = True
            self._executor.shutdown(cancel_futures=True)
        except Exception:
            self.stopping = True
            self._executor.shutdown(cancel_futures=True)
            raise

    def _update_progress(self, current_phase: InitPhase) -> None:
        lengths = {p: len(j) for p, j in self._jobs.items()}
        dones = {p: len(j) for p, j in self._jobs_done.items()}

        def done_ratio(phase):
            if phase < current_phase:
                return 1
            if phase in dones:
                return dones[phase] / (lengths.get(phase, 0) + dones[phase])
            return 0

        progress = int(sum(w * done_ratio(p)
                           for p, w in self.phase_weight.items()))

        self.progress_bar.update(progress - self.progress)
        self.progress_bar.set_postfix_str(str(current_phase))
        self.progress = progress


class InitManager:
    """Manager of swattool initialization.

    Coordinates the entire initialization process including:
    - Fetching failures data from swatbot server
    - Storing the data in a local SQLite database
    - Fetching additional information like bugzilla data and git repositories
    - Creating Build instances with all necessary information
    - Preparing log analysis for review mode
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, userinfos: userdata.UserInfos, limit: int,
                 filters: dict[str, Any], for_review: bool):
        self.userinfos = userinfos
        self.limit = limit
        self.filters = filters
        self.for_review = for_review

        self._db = database.Database()
        self._executor = InitExecutor(for_review)

        self._builds_ids = self._db.get_builds_ids()
        self._collections_ids = self._db.get_collections_ids()
        self._collections_fetch: set[int] = set()

        self._builds: list[swatbuild.Build] = []

    def _update_gits(self) -> None:
        try:
            pokyciarchive.update(min_age=10 * 60)
        except pygit2.GitError:
            logger.warning("Failed to update poky-ci-archive")

    def _update_failures(self
                         ) -> Optional[tuple[Callable,
                                             list[dict[str, Any]],
                                             Optional[swatbotrest.TriageStatus]
                                             ]]:
        statusfilter = None
        if len(self.filters.get('triage', [])) == 1:
            statusfilter = self.filters['triage'][0]
        failures = swatbotrest.get_stepfailures(statusfilter)
        if failures is None:
            return None

        return self._update_failures_done_cb, failures, statusfilter

    def _update_failures_done_cb(self, failures: list[dict[str, Any]],
                                 status: Optional[swatbotrest.TriageStatus]
                                 ) -> None:
        data = [{'failure_id': int(f['id']),
                 'build_id': int(f['relationships']['build']['data']['id']),
                 'step_number': f['attributes']['stepnumber'],
                 'step_name': f['attributes']['stepname'],
                 'urls': json.dumps({u.split()[0].rsplit('/')[-1]: u
                                     for u in
                                     f['attributes']['urls'].split()}),
                 'failure_status': f['attributes']['status'],
                 'remote_triage': f['attributes']['triage'],
                 'remote_triage_notes': f['attributes']['triagenotes']
                 }
                for f in failures]
        self._db.drop_failures(status)
        self._db.add_failures(data)

        build_failures: dict[int, dict[int, dict]] = {}
        for failure_data in failures:
            buildid = int(failure_data['relationships']['build']['data']['id'])
            failureid = int(failure_data['id'])
            build_failures.setdefault(buildid, {})[failureid] = failure_data

        limited_pending_ids = set(sorted(build_failures.keys(),
                                         reverse=True)[:self.limit])
        # Generate a list of all pending failures, fetching details from the
        # remote server as needed.
        for buildid in limited_pending_ids.difference(self._builds_ids):
            # Filter on status now, limiting the size of data we will have
            # to download from the server.
            if self.filters['triage']:
                triages = {f['attributes']['triage']
                           for f in build_failures[buildid].values()}

                if triages.isdisjoint(self.filters['triage']):
                    continue

            self._executor.submit(InitPhase.FAILURES_DATA, self._fetch_build,
                                  buildid)

    def _fetch_build(self, buildid: int
                     ) -> Optional[tuple[Callable, dict[str, Any]]]:
        build = swatbotrest.get_build(buildid)
        attributes = build['attributes']
        relationships = build['relationships']

        collectionid = relationships['buildcollection']['data']['id']

        data: dict[str, Any] = {}
        data['build_id'] = buildid
        data['buildbot_build_id'] = attributes['buildid']
        data['status'] = int(attributes['status'])
        data['test'] = attributes['targetname']
        data['worker'] = attributes['workername']
        data['completed'] = attributes['completed']
        data['collection_id'] = int(collectionid)
        data['ab_url'] = attributes['url']
        data['parent_id'] = None

        return self._fetch_build_done_cb, data

    def _fetch_build_done_cb(self, data: dict[str, Any]) -> None:
        self._db.add_build(data)
        self._trigger_fetch_collection(int(data['collection_id']),
                                       data['ab_url'])

    def _trigger_fetch_collection(self, collectionid: int, ab_url: str
                                  ) -> None:
        if (collectionid not in self._collections_ids
                and collectionid not in self._collections_fetch):
            aburl = buildbotrest.autobuilder_base_url(ab_url)
            buildboturl = buildbotrest.rest_api_url(aburl)
            self._executor.submit(InitPhase.COLLECTIONS_DATA,
                                  self._fetch_collection, collectionid,
                                  buildboturl)
            self._collections_fetch.add(collectionid)

    def _fetch_collection(self, collectionid: int, buildboturl: str
                          ) -> Optional[tuple[Callable, dict[str, Any]]]:
        collection = swatbotrest.get_build_collection(collectionid)

        build_id = collection['attributes']['buildid']
        build_data = buildbotrest.get_build(buildboturl, build_id)

        data: dict[str, Any] = {}
        data['collection_id'] = collectionid
        data['owner'] = collection['attributes']['owner']
        data['branch'] = collection['attributes']['branch']
        data['collection_build_id'] = collection['attributes']['buildid']
        data['target_name'] = collection['attributes']['targetname']
        data['parent_builder'] = data['parent_build_number'] = None
        for repo in swatbuild.ALL_REPOS:
            data[f'commit_{repo}'.replace('-', '_')] = None
        if build_data:
            build = build_data['builds'][0]
            data['parent_builder'] = build['builderid']
            data['parent_build_number'] = build['number']
            for repo in swatbuild.ALL_REPOS:
                rev = build['properties'].get(f'commit_{repo}')
                if rev:
                    data[f'commit_{repo}'.replace('-', '_')] = rev[0]

        return self._fetch_collection_done_cb, data

    def _fetch_collection_done_cb(self, data: dict[str, Any]) -> None:
        self._db.add_collection(data)

    def _update_bugzilla(self) -> None:
        Bugzilla.get_bugs()
        Bugzilla.get_bugs(abints=True)

    def _create_builds(self) -> None:
        failures = self._db.get_failures(self.filters['triage'],
                                         with_data=True)

        if self.for_review:
            build_ids = {f['buildbot_build_id'] for f in failures}
            logs_data = self._db.get_logs_data(build_ids)
            buildbotrest.populate_log_data_cache(logs_data)

        builds: dict[int, list[sqlite3.Row]] = {}
        for failure in failures:
            builds.setdefault(failure['build_id'], []).append(failure)

        for build_data in builds.values():
            build = swatbuild.Build(build_data)

            userinfo = self.userinfos[build.id]
            if not build.match_filters(self.filters, userinfo):
                continue

            self._builds.append(build)
            if self.for_review:
                self._executor.submit(InitPhase.LOGS, self._prepare_for_review,
                                      build)

    def _prepare_for_review(self, build: swatbuild.Build) -> None:
        swatlogs.Log(build.get_first_failure()).get_highlights()
        logfingerprint.get_log_fingerprint(build.get_first_failure())

    def _fetch_missing_data(self) -> None:
        # Database might miss some data because of previous fail fetches: add
        # them to the fetch list.
        miss_collections = self._db.get_missing_collections()
        for collectionid, ab_url in miss_collections:
            if _ab_url_is_valid(ab_url):
                self._trigger_fetch_collection(collectionid, ab_url)

    def run(self) -> None:
        """Run initialization tasks.

        Executes the complete initialization workflow:
        - Start background tasks for git and bugzilla data if in review mode.
        - Fetch missing data from previous incomplete runs.
        - Fetch failure list and related build data.
        - Create build objects from database records.
        - Prepare logs for review if needed
        """
        with logging_redirect_tqdm():
            if self.for_review:
                # This might be longer than everything else, especially if
                # there is only few new failures: Add it first.
                self._executor.submit(InitPhase.POKY_CI_ARCHIVE,
                                      self._update_gits)

                self._executor.submit(InitPhase.AB_INTS, self._update_bugzilla)

            self._fetch_missing_data()
            self._executor.submit(InitPhase.FAILURES_LIST,
                                  self._update_failures)

            try:
                self._executor.wait_phase_done(InitPhase.COLLECTIONS_DATA)
            finally:
                self._db.commit()

            self._create_builds()
            try:
                self._executor.wait_all()
            finally:
                self._db.commit()

            miss_failures = self._db.get_missing_failures()
            if miss_failures:
                logger.warning("Some failures were not fetched correctly: %s",
                               miss_failures)

            miss_collections = self._db.get_missing_collections()
            miss_collections_ids = [m[0] for m in miss_collections
                                    if _ab_url_is_valid(m[1])]
            if miss_collections:
                logger.warning("Some collections were not fetched correctly: "
                               "%s", miss_collections_ids)

            self._db.add_logs_data(buildbotrest.save_log_data_cache())
            self._db.commit()

    def get_builds(self, sort: Collection[swatbuild.Field]
                   ) -> list[swatbuild.Build]:
        """Get consolidated list of failure infos.

        Returns the list of builds sorted according to the specified fields.

        Args:
            sort: Collection of field names to sort by

        Returns:
            Sorted list of Build objects
        """
        if not self._executor.done:
            return []

        def sortfn(elem):
            return elem.get_sort_tuple(sort)

        return sorted(self._builds, key=sortfn)
