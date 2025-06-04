#!/usr/bin/env python3

"""Interaction with the swatbot Django server.

This module provides functionality for retrieving and processing build failures
from the swatbot server.
"""

import logging
from typing import Any, Collection

import sqlite3

from . import database
from . import logfingerprint
from . import swatbotrest
from . import swatbuild
from . import swatlogs
from . import userdata
from . import utils

logger = logging.getLogger(__name__)


# TODO: no longer fetches anything: rename to something else
# TODO: parallelism is probably not needed anymore
class BuildFetcher:
    """Consolidated list of failure infos generator.

    Retrieves build failure information from the swatbot server and
    processes it according to filters.
    """

    def __init__(self, userinfos: userdata.UserInfos, limit: int,
                 filters: dict[str, Any], preparelogs: bool = False):
        self.userinfos = userinfos
        self.limit = limit
        self.filters = filters
        self.preparelogs = preparelogs
        self.__infos: list[swatbuild.Build] = []
        self._db = database.Database()

    def _create_builds(self, executor: utils.ExecutorWithProgress):
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

            self.__infos.append(build)
            if self.preparelogs:
                swatlogs.Log(build.get_first_failure()).get_highlights()
                logfingerprint.get_log_fingerprint(build.get_first_failure())

    def prepare_with_executor(self, executor: utils.ExecutorWithProgress):
        """Prepare consolidated list of failure infos.

        Fetches failure information using a provided executor for concurrency.

        Args:
            executor: ExecutorWithProgress instance for parallel execution
        """
        self._create_builds(executor)

    def prepare(self):
        """Prepare consolidated list of failure infos.

        Creates an executor and uses it to fetch failure information.
        """
        executor = utils.ExecutorWithProgress()
        self.prepare_with_executor(executor)
        executor.run()

    def get_builds(self, sort: Collection[str],) -> list[swatbuild.Build]:
        """Get consolidated list of failure infos.

        Returns the list of builds sorted according to the specified fields.

        Args:
            sort: Collection of field names to sort by

        Returns:
            Sorted list of Build objects
        """

        def sortfn(elem):
            return elem.get_sort_tuple([swatbuild.Field(k) for k in sort])

        return sorted(self.__infos, key=sortfn)
