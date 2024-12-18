#!/usr/bin/env python3

"""Interaction with the buildbot server."""

import json
import logging
from typing import Any, Optional

import requests

from .webrequests import Session

logger = logging.getLogger(__name__)


def rest_api_url(base_url: str) -> str:
    """Get the REST API URL prefix for a given buildbot base URL."""
    return f"{base_url}/api/v2"


def _get_json(url) -> Optional[dict[str, Any]]:
    try:
        data = Session().get(url)
    except requests.exceptions.HTTPError:
        return None

    try:
        json_data = json.loads(data)
    except json.decoder.JSONDecodeError:
        return None

    return json_data


def get_build(rest_url: str, buildid: int) -> Optional[dict[str, Any]]:
    """Get data about a given build."""
    build_url = f"{rest_url}/builds/{buildid}"
    logging.debug("Build info URL: %s", build_url)

    return _get_json(build_url)


def get_log_raw_url(rest_url: str, buildid: int, stepnumber: int,
                    logname: str = "stdio") -> Optional[str]:
    """Get the URL of a raw log file."""
    info_url = f"{rest_url}/builds/{buildid}/steps/{stepnumber}/logs/{logname}"
    logging.debug("Log info URL: %s", info_url)

    info_data = _get_json(info_url)
    if not info_data:
        return None

    logid = info_data['logs'][0]['logid']
    return f"{rest_url}/logs/{logid}/raw"
