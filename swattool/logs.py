#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import json
import logging
from typing import Optional

from . import webrequests

logger = logging.getLogger(__name__)


TYPHOON_BASE_URL = "https://autobuilder.yoctoproject.org/typhoon"
TYPHOON_API_URL = f"{TYPHOON_BASE_URL}/api/v2"


def get_log_raw_url(buildid: int, stepid: int, logname: str) -> Optional[str]:
    info_url = f"{TYPHOON_API_URL}/builds/{buildid}/steps/{stepid}" \
               f"/logs/{logname}"

    info_data = webrequests.get(info_url)
    try:
        info_json_data = json.loads(info_data)
    except json.decoder.JSONDecodeError:
        return None

    logid = info_json_data['logs'][0]['logid']
    return f"{TYPHOON_API_URL}/logs/{logid}/raw"
