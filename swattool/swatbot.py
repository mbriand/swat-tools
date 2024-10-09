#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import logging
from typing import Any, Collection

import click

from . import swatbotrest
from . import swatbuild
from . import userdata

logger = logging.getLogger(__name__)


def get_failure_infos(limit: int, sort: Collection[str],
                      filters: dict[str, Any]
                      ) -> tuple[list[swatbuild.Build], userdata.UserInfos]:
    """Get consolidated list of failure infos and local reviews infos."""
    userinfos = userdata.UserInfos()

    logger.info("Loading build failures...")
    pending_failures = swatbotrest.get_pending_failures()

    # Generate a list of all pending failures, fetching details from the remote
    # server as needed.
    logger.info("Loading build failures details...")
    infos = []
    limited_pending_ids = sorted(pending_failures.keys(), reverse=True)[:limit]
    with click.progressbar(limited_pending_ids) as pending_ids_progress:
        for buildid in pending_ids_progress:
            build = swatbuild.Build(buildid, pending_failures[buildid])

            if build.match_filters(filters, userinfos[build.id]):
                infos.append(build)

    def sortfn(elem):
        return elem.get_sort_tuple([swatbuild.Field(k) for k in sort])

    return (sorted(infos, key=sortfn), userinfos)
