#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import concurrent.futures
import logging
from typing import Any, Collection, Optional

import click

from . import logsview
from . import swatbotrest
from . import swatbuild
from . import userdata

logger = logging.getLogger(__name__)


def _create_build(filters: dict[str, Any],
                  buildid: int,
                  failures: dict[int, dict],
                  userinfos: userdata.UserInfos
                  ) -> tuple[str, Optional[swatbuild.Build]]:
    build = swatbuild.Build(buildid, failures)

    userinfo = userinfos[build.id]
    if not build.match_filters(filters, userinfo):
        return ("build", None)

    return ("build", build)


def _prepare_log(build: swatbuild.Build):
    logsview.get_log_highlights(build.get_first_failure(), "stdio")
    return ("preparelogs", None)


def _create_builds(filters: dict[str, Any],
                   failures: dict[int, dict[int, dict]],
                   limit: int,
                   userinfos: userdata.UserInfos,
                   preparelogs: bool = False):
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

            jobs.append(executor.submit(_create_build, filters, buildid,
                                        failures[buildid], userinfos))

        try:
            progress = 0
            jobcount = len(jobs) * (2 if preparelogs else 1)
            with click.progressbar(length=jobcount,
                                   label="Loading build failures details"
                                   ) as jobsprogress:
                for future in concurrent.futures.as_completed(jobs):
                    restype, res = future.result()
                    if restype == "build":
                        if res is not None:
                            infos.append(res)
                            if preparelogs:
                                jobs.append(executor.submit(_prepare_log, res))
                                progress += 1
                            else:
                                progress += 2
                        else:
                            progress += 2
                    elif restype == "preparelogs":
                        progress += 1

                    jobsprogress.update(progress)
        except KeyboardInterrupt:
            executor.shutdown(cancel_futures=True)
            return ([], userinfos)
        except Exception:
            executor.shutdown(cancel_futures=True)
            raise

    return infos


def get_failure_infos(limit: int, sort: Collection[str],
                      filters: dict[str, Any], preparelogs: bool = False
                      ) -> tuple[list[swatbuild.Build], userdata.UserInfos]:
    """Get consolidated list of failure infos and local reviews infos."""
    userinfos = userdata.UserInfos()

    logger.info("Loading build failures...")

    statusfilter = None
    if len(filters.get('triage', [])) == 1:
        statusfilter = filters['triage'][0]
    failures = swatbotrest.get_failures(statusfilter)

    infos = _create_builds(filters, failures, limit, userinfos, preparelogs)

    def sortfn(elem):
        return elem.get_sort_tuple([swatbuild.Field(k) for k in sort])

    return (sorted(infos, key=sortfn), userinfos)
