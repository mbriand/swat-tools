#!/usr/bin/env python3

import concurrent.futures
import logging
import os
import sqlite3
from typing import Any, Collection
from typing import Iterable, Optional

import pygit2  # type: ignore

from .bugzilla import Bugzilla
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
                for fut in concurrent.futures.as_completed(self.jobs.keys()):
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
    def __init__(self, limit: int, filters: dict[str, Any], for_review: bool):
        self.limit = limit
        self.filters = filters
        self.for_review = for_review

        self._db = database.Database()
        self._executor = InitExecutor()

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

    def _update_failures_done_cb(self, failures):
        for failure_data in failures:
            buildid = int(failure_data['relationships']['build']['data']['id'])
            failureid = int(failure_data['id'])
            self._db.add_failure(failureid, buildid)

        self._create_db_update_jobs(failures)

    def _create_db_update_jobs(self, failures: list[dict[str, Any]]):
        # TODO: save failures in DB
        build_failures: dict[int, dict[int, dict]] = {}
        for failure_data in failures:
            buildid = int(failure_data['relationships']['build']['data']['id'])
            failureid = int(failure_data['id'])
            build_failures.setdefault(buildid, {})[failureid] = failure_data

        limited_pending_ids = sorted(build_failures.keys(),
                                     reverse=True)[:self.limit]
        # Generate a list of all pending failures, fetching details from the
        # remote server as needed.
        # TODO: remove ids already in db from the set to fetch
        for buildid in limited_pending_ids:
            # Filter on status now, limiting the size of data we will have
            # to download from the server.
            if self.filters['triage']:
                triages = {f['attributes']['triage']
                           for f in build_failures[buildid].values()}

                if triages.isdisjoint(self.filters['triage']):
                    continue

            cur = self._db.cursor()
            build_res = cur.execute("Select * from build WHERE id=?",
                                    (buildid,))
            if build_res.fetchone():
                continue
            cur.close()

            self._executor.submit("Fetching build data", self._fetch_build,
                                  buildid, build_failures[buildid])

    def _fetch_build(self, buildid, failures):
        build = swatbotrest.get_build(buildid)
        attributes = build['attributes']
        relationships = build['relationships']

        collectionid = relationships['buildcollection']['data']['id']
        collection = swatbotrest.get_build_collection(collectionid)
        # TODO: add collection in db

        data = {}
        data['id'] = attributes['buildid']
        data['status'] = int(attributes['status'])
        data['test'] = attributes['targetname']
        data['worker'] = attributes['workername']
        data['completed'] = attributes['completed']
        data['collection_id'] = collectionid
        data['ab_url'] = attributes['url']
        data['owner'] = collection['attributes']['owner']
        data['branch'] = collection['attributes']['branch']
        data['parent_id'] = None

        return self._fetch_build_done_cb, data

    def _fetch_build_done_cb(self, data):
        self._db.add_build(data)

    def run(self):
        if self.for_review:
            self._executor.submit("Fetching poky-ci-archive",
                                  self._update_gits)
            self._executor.submit("Updating AB-INT lists", Bugzilla.get_abints)

        self._executor.submit("Fetching failures", self._update_failures)

        self._executor.run()
        self._db.commit()


