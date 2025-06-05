#!/usr/bin/env python3

import concurrent.futures
import logging
import os
import sqlite3
from typing import Any, Collection
from typing import Iterable, Optional

import json
import pygit2  # type: ignore

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


# TODO: delete / merge with ExecutorWithProgress
class InitExecutor:
    def __init__(self, threads: Optional[int] = None):
        if threads is None:
            cpus = os.cpu_count()
            threads = min(16, cpus) if cpus else 16
        self.executor = concurrent.futures.ThreadPoolExecutor(threads)
        self.jobs: dict[concurrent.futures.Future, str] = {}

    def submit(self, name, fn, *args, **kwargs):
        """Submit a new job to the executor.

        Args:
            name: Display name for the job
            *args: Positional arguments for the job function
            **kwargs: Keyword arguments for the job function
        """
        self.jobs[self.executor.submit(fn, *args, **kwargs)] = name

    def run(self):
        try:
            while self.jobs.keys():
                done, _ = concurrent.futures.wait(self.jobs.keys(),
                                                  return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    err = fut.exception()
                    if err:
                        raise err
                    res = fut.result()
                    if res:
                        fn, *args = res
                        fn(*args)
                    del self.jobs[fut]
        except KeyboardInterrupt:
            self.executor.shutdown(cancel_futures=True)
        except Exception:
            self.executor.shutdown(cancel_futures=True)
            raise

class InitManager:
    def __init__(self, userinfos: userdata.UserInfos, limit: int,
                 filters: dict[str, Any], for_review: bool):
        self.userinfos = userinfos
        self.limit = limit
        self.filters = filters
        self.for_review = for_review

        self._db = database.Database()
        self._executor = InitExecutor()

        self._builds_ids = self._db.get_builds_ids()
        self._collections_ids = self._db.get_collections_ids()
        self._collections_fetch = set()

        self._builds: list[swatbuild.Build] = []

    def _update_gits(self):
        try:
            pokyciarchive.update(min_age=10 * 60)
        except pygit2.GitError:
            logger.warning("Failed to update poky-ci-archive")

    def _update_failures(self):
        statusfilter = None
        if len(self.filters.get('triage', [])) == 1:
            statusfilter = self.filters['triage'][0]
        failures = swatbotrest.get_stepfailures(statusfilter)
        if failures is None:
            return

        return self._update_failures_done_cb, failures

    def _update_failures_done_cb(self, failures: list[dict[str, Any]]):
        data = [{'failure_id': int(f['id']),
                 'build_id': int(f['relationships']['build']['data']['id']),
                 'step_number': f['attributes']['stepnumber'],
                 'step_name': f['attributes']['stepname'],
                 'urls': json.dumps({u.split()[0].rsplit('/')[-1]: u
                                     for u in
                                     f['attributes']['urls'].split()}),
                 'remote_triage': f['attributes']['triage'],
                 'remote_triage_notes': f['attributes']['triagenotes']
                 }
                for f in failures]
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

            self._executor.submit("Fetching build data", self._fetch_build,
                                  buildid, build_failures[buildid])

    def _fetch_build(self, buildid, failures):
        build = swatbotrest.get_build(buildid)
        attributes = build['attributes']
        relationships = build['relationships']

        collectionid = relationships['buildcollection']['data']['id']

        data = {}
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

    def _fetch_build_done_cb(self, data):
        self._db.add_build(data)
        collectionid = int(data['collection_id'])
        if (collectionid not in self._collections_ids and
                collectionid not in self._collections_fetch):
            aburl = buildbotrest.autobuilder_base_url(data['ab_url'])
            buildboturl = buildbotrest.rest_api_url(aburl)
            self._executor.submit("Fetching collection data",
                                  self._fetch_collection, collectionid,
                                  buildboturl)
            self._collections_fetch.add(collectionid)

    def _fetch_collection(self, collectionid: int, buildboturl: str):
        collection = swatbotrest.get_build_collection(collectionid)

        build_id = collection['attributes']['buildid']
        parent_build = buildbotrest.get_build(buildboturl, build_id)

        data = {}
        data['collection_id'] = collectionid
        data['owner'] = collection['attributes']['owner']
        data['branch'] = collection['attributes']['branch']
        data['collection_build_id'] = collection['attributes']['buildid']
        data['target_name'] = collection['attributes']['targetname']
        if parent_build:
            data['parent_builder'] = parent_build['builds'][0]['builderid']
            data['parent_build_number'] = parent_build['builds'][0]['number']

        return self._fetch_collection_done_cb, data

    def _fetch_collection_done_cb(self, data):
        self._db.add_collection(data)

    def _update_bugzilla(self) -> None:
        Bugzilla.get_abints()

    def _create_builds(self) -> None:
        failures = self._db.get_failures(self.filters['triage'],
                                         with_data=True)

        builds: dict[int, list[sqlite3.Row]] = {}
        for failure in failures.values():
            builds.setdefault(failure['build_id'], []).append(failure)

        for build_data in builds.values():
            build = swatbuild.Build(build_data)

            userinfo = self.userinfos[build.id]
            if not build.match_filters(self.filters, userinfo):
                continue

            self._builds.append(build)
            if self.for_review:
                self._executor.submit("Fetching logs",
                                      self._prepare_for_review, build)

    def _prepare_for_review(self, build: swatbuild.Build) -> None:
        swatlogs.Log(build.get_first_failure()).get_highlights()
        logfingerprint.get_log_fingerprint(build.get_first_failure())


    def run(self):
        if self.for_review:
            self._executor.submit("Fetching poky-ci-archive",
                                  self._update_gits)
            self._executor.submit("Updating AB-INT lists",
                                  self._update_bugzilla)

        self._executor.submit("Fetching failures", self._update_failures)

        self._executor.run()
        self._db.commit()

        # TODO: below must run after all swatbot/buildbot fetches are done, but
        # can be concurrent with ab-int/git fetches

        self._create_builds()
        self._executor.run()

    def get_builds(self, sort: Collection[str]) -> list[swatbuild.Build]:
        """Get consolidated list of failure infos.

        Returns the list of builds sorted according to the specified fields.

        Args:
            sort: Collection of field names to sort by

        Returns:
            Sorted list of Build objects
        """

        def sortfn(elem):
            return elem.get_sort_tuple([swatbuild.Field(k) for k in sort])

        return sorted(self._builds, key=sortfn)
