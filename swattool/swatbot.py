#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import concurrent.futures
import logging
from typing import Any, Collection

import click

from . import swatbotrest
from . import swatbuild
from . import userdata

logger = logging.getLogger(__name__)


def _create_builds(filters: dict[str, Any],
                   failures: dict[int, dict[int, dict]],
                   limit: int,
                   userinfos: userdata.UserInfos):
    logger.info("Loading build failures details...")
    infos = []
    limited_pending_ids = sorted(failures.keys(), reverse=True)[:limit]
    jobs = []

    # Generate a list of all pending failures, fetching details from the remote
    # server as needed.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for buildid in limited_pending_ids:
            # Filter on status now, limiting the size of data we will have to
            # download from the server.
            if filters['triage']:
                triages = {f['attributes']['triage']
                           for f in failures[buildid].values()}

                if triages.isdisjoint(filters['triage']):
                    continue

            jobs.append(executor.submit(swatbuild.Build, buildid,
                                        failures[buildid]))

        try:
            complete_iterator = concurrent.futures.as_completed(jobs)
            with click.progressbar(complete_iterator,
                                   length=len(jobs)) as jobsprogress:
                for future in jobsprogress:
                    build = future.result()
                    if build.match_filters(filters, userinfos[build.id]):
                        infos.append(build)
        except KeyboardInterrupt:
            executor.shutdown(cancel_futures=True)
            return ([], userinfos)
        except Exception:
            executor.shutdown(cancel_futures=True)
            raise

    return infos


def get_failure_infos(limit: int, sort: Collection[str],
                      filters: dict[str, Any]
                      ) -> tuple[list[swatbuild.Build], userdata.UserInfos]:
    """Get consolidated list of failure infos and local reviews infos."""
    userinfos = userdata.UserInfos()

    logger.info("Loading build failures...")

    statusfilter = None
    if len(filters.get('triage', [])) == 1:
        statusfilter = filters['triage'][0]
    failures = swatbotrest.get_failures(statusfilter)

    infos = _create_builds(filters, failures, limit, userinfos)

    def sortfn(elem):
        return elem.get_sort_tuple([swatbuild.Field(k) for k in sort])

    return (sorted(infos, key=sortfn), userinfos)
