#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import json
import logging
from typing import Optional

import requests

from .webrequests import Session

logger = logging.getLogger(__name__)


TYPHOON_BASE_URL = "https://autobuilder.yoctoproject.org/typhoon"
TYPHOON_API_URL = f"{TYPHOON_BASE_URL}/api/v2"


def get_log_raw_url(buildid: int, stepnumber: int, logname: str
                    ) -> Optional[str]:
    """Get URL of a raw log file, based on build and step ids."""
    info_url = f"{TYPHOON_API_URL}/builds/{buildid}/steps/{stepnumber}" \
               f"/logs/{logname}"

    try:
        info_data = Session().get(info_url)
    except requests.exceptions.HTTPError:
        return None

    try:
        info_json_data = json.loads(info_data)
    except json.decoder.JSONDecodeError:
        return None

    logid = info_json_data['logs'][0]['logid']
    return f"{TYPHOON_API_URL}/logs/{logid}/raw"
