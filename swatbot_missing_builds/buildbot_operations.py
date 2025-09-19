#!/usr/bin/env python3

"""Buildbot-related operations for swatbot_missing_builds tool."""

import datetime
import json
import logging
from enum import Enum
from typing import Optional


from swattool import swatbotrest
from swattool.webrequests import Session

logger = logging.getLogger(__name__)


class BuildStatus(Enum):
    """Status of a build check result."""

    UP_TO_DATE = "up_to_date"
    MISSING = "missing"
    NEEDS_UPDATE = "needs_update"
    IGNORED = "ignored"


def get_build_collection(rest_url: str, build: dict) -> int:
    """Get the collection build ID for a given build.

    Args:
        rest_url: The REST API URL prefix
        build: Build dictionary containing build information

    Returns:
        The collection build ID
    """
    buildrequestid = build['buildrequestid']
    buildreq_url = f"{rest_url}/buildrequests/{buildrequestid}"
    buildreq_json = Session().get(buildreq_url, True, -1)
    buildreq_data = json.loads(buildreq_json)
    buildreq = buildreq_data['buildrequests'][0]

    buildsetid = buildreq['buildsetid']
    buildset_url = f"{rest_url}/buildsets/{buildsetid}"
    buildset_json = Session().get(buildset_url, True, -1)
    buildset_data = json.loads(buildset_json)
    buildset = buildset_data['buildsets'][0]

    collection_build_id = buildset['parent_buildid']
    if not collection_build_id:
        collection_build_id = build['buildid']

    return collection_build_id


def get_build_branch(build: dict) -> Optional[str]:
    """Extract branch name from build properties.

    Args:
        build: Build dictionary containing properties

    Returns:
        Branch name if found, None otherwise
    """
    repos = ['poky', 'oecore', 'yocto-docs']
    for repo in repos:
        branch = build['properties'].get(f'branch_{repo}')
        if branch:
            return branch[0]
    branches = [p for p in build['properties'] if p.startswith('branch')]
    logging.warning("Failed to get corresponding branch, possible values: %s",
                    branches)
    return None


def get_step_urls(rest_url: str, buildbot_url: str, build: dict, step: dict
                  ) -> list[str]:
    """Get URLs for all logs of a build step.

    Args:
        rest_url: The REST API URL prefix
        buildbot_url: The buildbot base URL
        build: Build dictionary
        step: Step dictionary

    Returns:
        List of log URLs for the step
    """
    buildid = build['buildid']

    logs_url = f"{rest_url}/builds/{buildid}/steps/{step['number']}/logs"
    logs_json = Session().get(logs_url, True, 0)
    logs_data = json.loads(logs_json)
    logs = logs_data['logs']

    builderid = build['builderid']
    number = build['number']
    build_url = f"{buildbot_url}#/builders/{builderid}/builds/{number}"
    prefix = f"{build_url}/steps/{step['number']}/logs"
    return [f"{prefix}/{log['name'].replace(' ', '_')}" for log in logs]


def check_build_is_missing(base_url, rest_url: str, buildid: int
                           ) -> BuildStatus:
    """Check if a build is missing from swatbot or needs updating.

    Args:
        rest_url: The REST API URL prefix
        buildid: The build ID to check

    Returns:
        BuildStatus indicating the build's status
    """
    cache_max_age = 60 * 60

    build_url = f"{rest_url}/builds/{buildid}?property=swat_monitor"
    build_json = Session().get(build_url, True, cache_max_age)
    build_data = json.loads(build_json)
    build = build_data['builds'][0]
    if not build['complete_at']:
        logger.warning("Ignoring build %s as no end time was set", buildid)
        return BuildStatus.IGNORED
    build_time = datetime.datetime.fromtimestamp(build['complete_at'],
                                                 datetime.timezone.utc)

    sb_builds = swatbotrest.get_json(f"/build/?buildid={buildid}",
                                     cache_max_age)['data']
    build_path = f"/builders/{build['builderid']}/builds/{build['number']}"
    build_url = f"{base_url}/#{build_path}"
    if len(sb_builds) >= 1:
        if len(sb_builds) != 1:
            logger.warning("Unexpected number of entries found on swatbot "
                           "for build %s: %s", buildid, len(sb_builds))
            return BuildStatus.IGNORED

        sb_complete = sb_builds[0]['attributes']['completed']
        sb_build_time = None
        if sb_complete:
            sb_build_time = datetime.datetime.fromisoformat(sb_complete)

        if sb_build_time == build_time:
            logger.debug("Build %s found on swatbot", buildid)
            return BuildStatus.UP_TO_DATE
        logger.info("Build %s found on swatbot but with %s complete time "
                    "instead of %s: %s", buildid, sb_build_time, build_time,
                    build_url)
        return BuildStatus.NEEDS_UPDATE

    logger.info("Build %s has to be sent to swatbot: %s", buildid, build_url)
    return BuildStatus.MISSING
