#!/usr/bin/env python3

import logging
import json
import requests

logger = logging.getLogger(__name__)

REST_BASE_URL = "https://bugzilla.yoctoproject.org/rest/"

KNOWN_ABINTS = None


def get_abints() -> dict[int, str]:
    global KNOWN_ABINTS
    if not KNOWN_ABINTS:
        logger.info("Loading AB-INT list...")
        params = {
            'order': 'order=bug_id%20DESC',
            'query_format': 'advanced',
            'resolution': '---',
            'short_desc': 'AB-INT%3A.*',
            'short_desc_type': 'regexp',
            'include_fields': 'id,summary',
        }

        fparams = [f'{k}={v}' for k, v in params.items()]
        req = f"{REST_BASE_URL}bug?{'&'.join(fparams)}"
        r = requests.get(req)
        r.raise_for_status()

        KNOWN_ABINTS = {bug['id']: bug['summary']
                        for bug in json.loads(r.text)['bugs']}

    return KNOWN_ABINTS
