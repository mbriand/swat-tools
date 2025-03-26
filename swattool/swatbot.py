#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import logging
from typing import Any, Collection

from . import logsview
from . import swatbotrest
from . import swatbuild
from . import userdata
from . import utils

logger = logging.getLogger(__name__)


class BuildFetcher:
    """Consolidated list of failure infos generator."""

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
            logsview.get_log_highlights(build.get_first_failure(), "stdio")

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
        """Prepare consolidated list of failure infos."""
        statusfilter = None
        if len(self.filters.get('triage', [])) == 1:
            statusfilter = self.filters['triage'][0]
        failures = swatbotrest.get_failures(statusfilter)

        self._create_builds(failures, executor)

    def prepare(self):
        """Prepare consolidated list of failure infos."""
        executor = utils.ExecutorWithProgress()
        self.prepare_with_executor(executor)
        executor.run()

    def get_builds(self, sort: Collection[str],) -> list[swatbuild.Build]:
        """Get consolidated list of failure infos."""

        def sortfn(elem):
            return elem.get_sort_tuple([swatbuild.Field(k) for k in sort])

        return sorted(self.__infos, key=sortfn)
