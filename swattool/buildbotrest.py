#!/usr/bin/env python3

"""Interaction with the buildbot server.

This module provides functions for accessing the buildbot REST API
to retrieve build information and log files.
"""

import json
import logging
from typing import Any, Optional

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
    url, _, _ = autobuilder_url.partition('/#/builders')
    return url


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


def get_build(rest_url: str, buildid: int) -> Optional[dict[str, Any]]:
    """Get data about a given build.

    Retrieves build information from the buildbot REST API.

    Args:
        rest_url: The REST API URL prefix
        buildid: The ID of the build to retrieve

    Returns:
        Dictionary containing build information or None if request fails
    """
    build_url = f"{rest_url}/builds/{buildid}?property=*"
    logger.debug("Build info URL: %s", build_url)

    return _get_json(build_url)


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
    # TODO: store this in DB
    info_url = f"{rest_url}/builds/{buildid}/steps/{stepnumber}/logs/{logname}"
    logger.debug("Log info URL: %s", info_url)

    info_data = _get_json(info_url)
    if not info_data:
        return None

    return info_data['logs'][0]
