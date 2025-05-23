#!/usr/bin/env python3

"""Interaction with the swatbot Django server.

This module provides functionality for retrieving and processing build failures
from the swatbot server.
"""

import logging
from typing import Any, Collection

from . import logfingerprint
from . import swatbotrest
from . import swatbuild
from . import swatlogs
from . import userdata
from . import utils

logger = logging.getLogger(__name__)


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

    def _create_build(self, buildid: int, failures: dict[int, dict]) -> None:
        build = swatbuild.Build(buildid, failures)

        userinfo = self.userinfos[build.id]
        if not build.match_filters(self.filters, userinfo):
            return

        self.__infos.append(build)
        if self.preparelogs:
            swatlogs.Log(build.get_first_failure()).get_highlights()
            logfingerprint.get_log_fingerprint(build.get_first_failure())

    def _create_builds(self, failures: dict[int, dict[int, dict]],
                       executor: utils.ExecutorWithProgress):
        limited_pending_ids = sorted(failures.keys(),
                                     reverse=True)[:self.limit]
        # Generate a list of all pending failures, fetching details from the
        # remote server as needed.
        for buildid in limited_pending_ids:
            # Filter on status now, limiting the size of data we will have
            # to download from the server.
            if self.filters['triage']:
                triages = {f['attributes']['triage']
                           for f in failures[buildid].values()}

                if triages.isdisjoint(self.filters['triage']):
                    continue

            executor.submit("Fetching builds data", self._create_build,
                            buildid, failures[buildid])

    def prepare_with_executor(self, executor: utils.ExecutorWithProgress):
        """Prepare consolidated list of failure infos.

        Fetches failure information using a provided executor for concurrency.

        Args:
            executor: ExecutorWithProgress instance for parallel execution
        """
        statusfilter = None
        if len(self.filters.get('triage', [])) == 1:
            statusfilter = self.filters['triage'][0]
        failures = swatbotrest.get_failures(statusfilter)

        self._create_builds(failures, executor)

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
