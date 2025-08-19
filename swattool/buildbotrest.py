#!/usr/bin/env python3

"""Interaction with the buildbot server.

This module provides functions for accessing the buildbot REST API
to retrieve build information and log files.
"""

import json
import logging
import sqlite3
from typing import Any, Optional
import urllib

import requests

from .webrequests import Session
from . import utils

logger = logging.getLogger(__name__)


def rest_api_url(base_url: str) -> str:
    """Get the REST API URL prefix for a given buildbot base URL.

    Args:
        base_url: The base URL of the buildbot server

    Returns:
        The REST API URL prefix for the buildbot server
    """
    return f"{base_url}/api/v2"


def autobuilder_base_url(autobuilder_url) -> str:
    """Retrieve the autobuilder base URL from a full URL.

    Extracts the base URL by removing the UI-specific path components.

    Args:
        autobuilder_url: A full autobuilder URL, possibly including UI path

    Returns:
        The base URL without UI-specific components
    """
    for sep in ['/#/builders', '/#builders']:
        if sep in autobuilder_url:
            autobuilder_url, _, _ = autobuilder_url.partition(sep)
            break
    return autobuilder_url


ab_short_names = {
    'autobuilder.yoctoproject.org/typhoon': 'ty',
    'autobuilder.yoctoproject.org/valkyrie': 'vk',
    'valkyrie.yoctoproject.org': 'vk',
}


def autobuilder_short_name(autobuilder_url) -> str:
    """Retrieve the autobuilder short name from an URL.

    Args:
        autobuilder_url: A full autobuilder URL, possibly including UI path

    Returns:
        The autobuilder instance short name
    """
    url = urllib.parse.urlparse(autobuilder_base_url(autobuilder_url))
    ab_name = f'{url.netloc}{url.path}'
    return ab_short_names.get(ab_name, ab_name)


def _get_json(url) -> Optional[dict[str, Any]]:
    try:
        data = Session().get(url)
    except (requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError) as err:
        raise utils.SwattoolException(f"Failed to fetch {url}") from err

    try:
        json_data = json.loads(data)
    except json.decoder.JSONDecodeError:
        return None

    return json_data


def _fix_build_id(buildid: int) -> int:
    # Fix builds with faked ids (old buildbot instances).
    if buildid >= 2000000000000:
        buildid -= 2000000000000

    return buildid


def get_build(rest_url: str, buildid: int) -> Optional[dict[str, Any]]:
    """Get data about a given build.

    Retrieves build information from the buildbot REST API.

    Args:
        rest_url: The REST API URL prefix
        buildid: The ID of the build to retrieve

    Returns:
        Dictionary containing build information or None if request fails
    """
    buildid = _fix_build_id(buildid)

    build_url = f"{rest_url}/builds/{buildid}?property=*"
    logger.debug("Build info URL: %s", build_url)

    return _get_json(build_url)


_log_data_cache: dict[tuple[str, int, int, str], dict[str, Any]] = {}
_log_data_cache_new: set[tuple[str, int, int, str]] = set()


def populate_log_data_cache(data: list[sqlite3.Row]):
    """Load log data cache from database rows.

    Args:
        data: List of database rows containing log metadata
    """
    for row in data:
        key = (row["ab_instance"], row["build_id"], row["step_number"],
               row["logname"])
        _log_data_cache[key] = {
            "logid": row["logid"],
            "num_lines": row["num_lines"],
            "name": row["logname"],
        }


def save_log_data_cache() -> list[dict[str, Any]]:
    """Get new cache entries for database storage.

    Returns:
        List of dictionaries containing new log data cache entries
    """
    new_data = [{"ab_instance": k[0],
                 "build_id": k[1],
                 "step_number": k[2],
                 "logname": _log_data_cache[k]["name"],
                 **_log_data_cache[k],
                 } for k in _log_data_cache_new]
    _log_data_cache_new.clear()
    return new_data


def get_log_data(rest_url: str, buildid: int, stepnumber: int,
                 logname: str = "stdio") -> Optional[dict[str, Any]]:
    """Get the metadata of a log file.

    Args:
        rest_url: The REST API URL prefix
        buildid: The ID of the build
        stepnumber: The step number within the build
        logname: The name of the log file (default: "stdio")

    Returns:
        Dictionary containing log metadata or None if request fails
    """
    ab_instance = autobuilder_short_name(rest_url)
    cache_key = (ab_instance, buildid, stepnumber, logname)
    metadata = _log_data_cache.get(cache_key)
    if metadata:
        return metadata

    buildid = _fix_build_id(buildid)
    info_url = f"{rest_url}/builds/{buildid}/steps/{stepnumber}/logs/{logname}"
    logger.debug("Log info URL: %s", info_url)

    try:
        info_data = _get_json(info_url)
    except utils.SwattoolException as err:
        logger.warning("Got exception while trying to load logs: %s", err)
        return None
    if not info_data:
        return None

    _log_data_cache[cache_key] = info_data['logs'][0]
    _log_data_cache_new.add(cache_key)
    return info_data['logs'][0]
