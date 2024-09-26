#!/usr/bin/env python3

"""Bugzilla related functions."""

import logging
import json

from . import webrequests

logger = logging.getLogger(__name__)

BASE_URL = "https://bugzilla.yoctoproject.org/rest/"
REST_BASE_URL = f"{BASE_URL}/rest/"

KNOWN_ABINTS = None


def get_abints() -> dict[int, str]:
    """Get a dictionarry of all AB-INT issues currently open."""
    global KNOWN_ABINTS
    if not KNOWN_ABINTS:
        logger.info("Loading AB-INT list...")
        params = {
            'order': 'order=bug_id%20DESC',
            'query_format': 'advanced',
            'resolution': '---',
            'short_desc': 'AB-INT.*',
            'short_desc_type': 'regexp',
            'include_fields': 'id,summary',
        }

        fparams = [f'{k}={v}' for k, v in params.items()]
        req = f"{REST_BASE_URL}bug?{'&'.join(fparams)}"
        data = webrequests.get(req)

        KNOWN_ABINTS = {bug['id']: bug['summary']
                        for bug in json.loads(data)['bugs']}

    return KNOWN_ABINTS


def get_bug_url(bugid: int) -> str:
    """Get the bugzilla URL corresponding to a given issue ID."""
    return f"{BASE_URL}/show_bug.cgi?id={bugid}"


def add_bug_comment(bugid: int, comment: str):
    """Publish a new comment to a bugzilla issue."""
    bugurl = get_bug_url(bugid)

    # TODO: remove and publish using REST API
    print(f"\nPlease update {bugurl} ticket id with:\n"
          f"{'-'*40}\n"
          f"{comment}\n"
          f"{'-'*40}\n")
