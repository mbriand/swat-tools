#!/usr/bin/env python3

"""Bugzilla related functions."""

import urllib
import logging
import json
from typing import Optional

from .webrequests import Session

logger = logging.getLogger(__name__)

BASE_URL = "https://bugzilla.yoctoproject.org"
REST_BASE_URL = f"{BASE_URL}/rest/"


class Bugzilla:
    """Bugzilla server interaction class."""

    CACHE_TIMEOUT_S = 60 * 10

    known_abints: dict[int, str] = {}

    @classmethod
    def get_abints(cls) -> dict[int, str]:
        """Get a dictionarry of all AB-INT issues currently open."""
        if not cls.known_abints:
            logger.info("Loading AB-INT list...")
            params = {
                'order': 'order=bug_id%20DESC',
                'query_format': 'advanced',
                'resolution': ["---",
                               "FIXED",
                               "INVALID",
                               "OBSOLETE",
                               "NOTABUG",
                               "ReportedUpstream",
                               "WONTFIX",
                               "WORKSFORME",
                               "MOVED",
                               ],
                # 'short_desc': 'AB-INT.*',
                # 'short_desc_type': 'regexp',
                'status_whiteboard': 'AB-INT',
                'status_whiteboard_type': 'allwordssubstr',
                'include_fields': ['id', 'summary'],
            }

            fparams = urllib.parse.urlencode(params, doseq=True)
            req = f"{REST_BASE_URL}bug?{fparams}"
            data = Session().get(req, cls.CACHE_TIMEOUT_S)

            cls.known_abints = {bug['id']: bug['summary']
                                for bug in json.loads(data)['bugs']}

        return cls.known_abints

    @classmethod
    def get_bug_url(cls, bugid: int) -> str:
        """Get the bugzilla URL corresponding to a given issue ID."""
        return f"{BASE_URL}/show_bug.cgi?id={bugid}"

    @classmethod
    def get_bug_title(cls, bugid: int) -> Optional[str]:
        """Get bugzilla bug title."""
        abints = cls.get_abints()
        if bugid in abints:
            return abints[bugid]

        # order=order=bug_id DESC&query_format=advanced&bug_id=15614
        params = {
            'order': 'order=bug_id%20DESC',
            'query_format': 'advanced',
            'bug_id': bugid,
        }

        fparams = urllib.parse.urlencode(params, doseq=True)
        req = f"{REST_BASE_URL}bug?{fparams}"
        data = Session().get(req, cls.CACHE_TIMEOUT_S)

        jsondata = json.loads(data)['bugs']
        if len(jsondata) != 1:
            return None

        return jsondata[0]['summary']

    @classmethod
    def add_bug_comment(cls, bugid: int, comment: str):
        """Publish a new comment to a bugzilla issue."""
        bugurl = cls.get_bug_url(bugid)

        # TODO: remove and publish using REST API
        print(f"\nPlease update {bugurl} ticket id with:\n"
              f"{'-'*40}\n"
              f"{comment}\n"
              f"{'-'*40}\n")
