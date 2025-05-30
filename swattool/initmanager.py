#!/usr/bin/env python3

import concurrent.futures
import logging
import os
import sqlite3
from typing import Any, Collection
from typing import Iterable, Optional

import pygit2  # type: ignore

from .bugzilla import Bugzilla
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
        self.jobs: list[tuple[str, concurrent.futures.Future]] = []

    def submit(self, name, *args, **kwargs):
        """Submit a new job to the executor.

        Args:
            name: Display name for the job
            *args: Positional arguments for the job function
            **kwargs: Keyword arguments for the job function
        """
        self.jobs.append((name, self.executor.submit(*args, **kwargs)))

    def run(self):
        try:
            alljobs = [job[1] for job in self.jobs]
            for _ in concurrent.futures.as_completed(alljobs):
                pass
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

        self._executor = InitExecutor()

        self._db = sqlite3.connect(utils.DATADIR / "swattool.db")
        cur = self._db.cursor()
        cur.execute("CREATE TABLE build(id PRIMARY KEY, status, test, worker, "
                    "completed, collection_id, ab_url, owner, branch, "
                    "parent_id)")
        cur.execute("CREATE TABLE collection(id PRIMARY KEY, "
                    "owner, branch, build_id)")

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

        cur = self._db.cursor()
        for failure_data in failures:
            buildid = int(failure_data['relationships']['build']['data']['id'])
            failureid = int(failure_data['id'])
            cur.execute("INSERT INTO failures(id, build_id)"
                        "VALUES(?, ?) ON CONFLICT(id)"
                        "DO NOTHING", (failureid, buildid))

        self._create_db_update_jobs()

    def _create_db_update_jobs(self):
        # TODO: save failures in DB

        limited_pending_ids = sorted(failures.keys(),
                                     reverse=True)[:self.limit]
        # Generate a list of all pending failures, fetching details from the
        # remote server as needed.
        # TODO: remove ids already in db from the set to fetch
        for buildid in limited_pending_ids:
            # Filter on status now, limiting the size of data we will have
            # to download from the server.
            if self.filters['triage']:
                triages = {f['attributes']['triage']
                           for f in failures[buildid].values()}

                if triages.isdisjoint(self.filters['triage']):
                    continue

            cur = self._db.cursor()
            build_res = cur.execute("Select * from build WHERE buildid=?",
                                    (buildid,))
            if build_res.rowcount:
                print(f"Found build {buildid}")
                continue

            self._executor.submit("Fetching build data", self._fetch_build,
                                  buildid, failures[buildid])

    def _fetch_build(self):
        pass

    def run(self):
        if self.for_review:
            self._executor.submit("Fetching poky-ci-archive",
                                  self._update_gits)
            self._executor.submit("Updating AB-INT lists", Bugzilla.get_abints)

        self._executor.submit("Fetching failures", self._update_failures)

        self._executor.run()


