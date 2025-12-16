#!/usr/bin/env python3

"""Swatbot API operations for swatbot_missing_builds tool."""

import datetime
import json
import logging
from typing import Any, Optional

from buildbot.process import results  # type: ignore

from swattool import swatbotrest
from swattool.webrequests import Session
from . import buildbot_operations

logger = logging.getLogger(__name__)


def get_or_add_collection_id(
    rest_url: str, build: dict, dry_run: bool
) -> Optional[int]:
    """Get existing collection ID or add new collection for a build.

    Args:
        rest_url: The REST API URL prefix
        build: Build dictionary containing build information
        dry_run: If True, don't actually create collections

    Returns:
        Collection ID if successful, None otherwise
    """
    collection_build_id = buildbot_operations.get_build_collection(
        rest_url, build
    )

    if collection_build_id != build["buildid"]:
        build_url = f"{rest_url}/builds/{collection_build_id}?property=*"
        build_json = Session().get(build_url, True, 0)
        build_data = json.loads(build_json)
        collection_build = build_data["builds"][0]
    else:
        collection_build = build

    sb_collections_url = f"/buildcollection/?buildid={collection_build_id}"
    sb_collections = swatbotrest.get_json(sb_collections_url, 0)["data"]
    if len(sb_collections) > 1:
        logger.error(
            "Unexpected number of collections for buildid %s: %s",
            collection_build_id,
            len(sb_collections),
        )
        return None
    if len(sb_collections) == 1:
        return sb_collections[0]["id"]

    logger.info("Adding build collection %s", collection_build_id)
    col_props = collection_build["properties"]
    branch = buildbot_operations.get_build_branch(collection_build)
    if not branch:
        logger.warning(
            "Failed to get branch of build %s: cannot create build collection",
            collection_build_id,
        )
        return None

    payload: dict[str, dict[str, Any]] = {
        "data": {
            "type": "BuildCollection",
            "attributes": {
                "buildid": collection_build_id,
                "targetname": col_props["buildername"][0],
                "branch": branch,
            },
        }
    }

    if "reason" in col_props and col_props["reason"][0]:
        payload["data"]["attributes"]["reason"] = col_props["reason"][0]
    if "owner" in col_props and col_props["owner"][0]:
        payload["data"]["attributes"]["owner"] = col_props["owner"][0]
    if "swat_monitor" in col_props and col_props["swat_monitor"][0]:
        payload["data"]["attributes"]["forswat"] = True
    else:
        payload["data"]["attributes"]["forswat"] = False

    if dry_run:
        logger.info(
            "Would have sent to %s: %s", "rest/buildcollection/", payload
        )
        data = {"id": 123456}
    else:
        data = swatbotrest.post_json("/buildcollection/", payload)["data"]
    return data["id"]


def add_build(
    rest_url: str, buildbot_url: str, buildid: int, dry_run: bool
) -> bool:
    """Add a build to swatbot.

    Args:
        rest_url: The REST API URL prefix
        buildbot_url: The buildbot base URL
        buildid: The build ID to add
        dry_run: If True, don't actually add builds

    Returns:
        True if successful, False otherwise
    """
    build_url = f"{rest_url}/builds/{buildid}?property=*"
    build_json = Session().get(build_url, True, 0)
    build_data = json.loads(build_json)
    build = build_data["builds"][0]

    sb_builds = swatbotrest.get_json(f"/build/?buildid={buildid}", 0)["data"]
    if len(sb_builds) >= 1:
        logger.warning(
            "Build %s found on swatbot, we will not add it", buildid
        )
        return False

    collection_id = get_or_add_collection_id(rest_url, build, dry_run)
    if not collection_id:
        logger.warning(
            "Failed to get collection for build %s: skipping", buildid
        )
        return False

    logger.info("Adding build %s", buildid)
    builderid = build["builderid"]
    number = build["number"]
    build_url = f"{buildbot_url}/#/builders/{builderid}/builds/{number}"
    started_at = datetime.datetime.fromtimestamp(
        build["started_at"], datetime.timezone.utc
    )

    if "buildername" not in build["properties"]:
        logging.warning("Builder name not set: was the build cancelled?")
        return False

    payload = {
        "data": {
            "type": "Build",
            "attributes": {
                "buildid": build["buildid"],
                "url": build_url,
                "targetname": build["properties"]["buildername"][0],
                "started": started_at.isoformat(),
                "workername": build["properties"]["workername"][0],
                "buildcollection": {
                    "type": "BuildCollection",
                    "id": collection_id,
                },
            },
        }
    }

    if dry_run:
        logger.info("Would have sent to %s: %s", "rest/build/", payload)
        return False

    swatbotrest.post_json("/build/", payload)
    return True


def add_build_steps(
    rest_url: str,
    buildbot_url: str,
    build: dict,
    sb_buildid: int,
    dry_run: bool,
) -> None:
    """Add build step failures to swatbot.

    Args:
        rest_url: The REST API URL prefix
        buildbot_url: The buildbot base URL
        build: Build dictionary
        sb_buildid: The swatbot build ID
        dry_run: If True, don't actually add step failures
    """
    buildid = build["buildid"]

    steps_url = f"{rest_url}/builds/{buildid}/steps"
    steps_json = Session().get(steps_url, True, 0)
    steps_data = json.loads(steps_json)
    for step in steps_data["steps"]:
        # Ignore logs for steps which succeeded/cancelled
        result = step["results"]
        if result in (
            results.SUCCESS,
            results.RETRY,
            results.CANCELLED,
            results.SKIPPED,
        ):
            continue

        # Log for FAILURE, EXCEPTION, WARNING
        urls = buildbot_operations.get_step_urls(
            rest_url, buildbot_url, build, step
        )
        if not urls:
            logger.warning(
                "Skipping step %s of build %s as there is no log",
                step["number"],
                buildid,
            )
            continue

        payload = {
            "data": {
                "type": "StepFailure",
                "attributes": {
                    "urls": " ".join(urls),
                    "status": step["results"],
                    "stepname": step["name"],
                    "stepnumber": step["number"],
                    "build": {
                        "type": "Build",
                        "id": sb_buildid,
                    },
                },
            }
        }

        if dry_run:
            logger.info(
                "Would have sent to %s: %s", "rest/stepfailure/", payload
            )
        else:
            swatbotrest.post_json("/stepfailure/", payload)


def update_build(
    rest_url: str, buildbot_url: str, buildid: int, dry_run: bool
) -> None:
    """Update an existing build in swatbot.

    Args:
        rest_url: The REST API URL prefix
        buildbot_url: The buildbot base URL
        buildid: The build ID to update
        dry_run: If True, don't actually update builds
    """
    build_url = f"{rest_url}/builds/{buildid}?property=swat_monitor"
    build_json = Session().get(build_url, True, 0)
    build_data = json.loads(build_json)
    build = build_data["builds"][0]

    sb_builds = swatbotrest.get_json(f"/build/?buildid={buildid}", 0)["data"]
    if len(sb_builds) != 1:
        logger.warning(
            "Unexpected number of entries found on swatbot for build %s: %s",
            buildid,
            len(sb_builds),
        )
        return

    logger.info("Updating build %s", buildid)
    sb_buildid = sb_builds[0]["id"]

    payload = {
        "data": {
            "id": sb_buildid,
            "type": "Build",
            "attributes": sb_builds[0]["attributes"],
        }
    }

    payload["data"]["attributes"]["status"] = build["results"]
    complete_at = datetime.datetime.fromtimestamp(
        build["complete_at"], datetime.timezone.utc
    )
    payload["data"]["attributes"]["completed"] = complete_at.isoformat()
    if "yp_build_revision" in build["properties"]:
        revision = build["properties"]["yp_build_revision"][0]
        payload["data"]["attributes"]["revision"] = revision

    if dry_run:
        logger.info("Would have sent to %s: %s", "rest/build/", payload)
    else:
        swatbotrest.put_json(f"/build/{sb_buildid}/", payload)

    add_build_steps(rest_url, buildbot_url, build, sb_buildid, dry_run)
