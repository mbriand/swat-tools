#!/usr/bin/env python3

"""Bugzilla related functions."""

import urllib
import logging
import json
from typing import Optional

import requests

from .webrequests import Session
from . import utils

logger = logging.getLogger(__name__)

BASE_URL = "https://bugzilla.yoctoproject.org"
REST_BASE_URL = f"{BASE_URL}/rest/"
ISSUE_URL = f"{BASE_URL}/show_bug.cgi?id="

TOKENFILE = utils.DATADIR / 'bugzilla_token'


class Bugzilla:
    """Bugzilla server interaction class."""

    CACHE_TIMEOUT_S = 60 * 60 * 24

    known_abints: dict[int, str] = {}

    @classmethod
    def get_abints(cls, force_refresh: bool = False) -> dict[int, str]:
        """Get a dictionarry of all AB-INT issues currently open."""
        if not cls.known_abints or force_refresh:
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
            cache_timeout = 0 if force_refresh else cls.CACHE_TIMEOUT_S
            data = Session().get(req, cache_timeout)

            cls.known_abints = {bug['id']: bug['summary']
                                for bug in json.loads(data)['bugs']}

        return cls.known_abints

    @classmethod
    def get_bug_url(cls, bugid: int) -> str:
        """Get the bugzilla URL corresponding to a given issue ID."""
        return f"{ISSUE_URL}{bugid}"

    @classmethod
    def get_bug_id_from_url(cls, bugurl: str) -> Optional[int]:
        """Get the bugzilla issue ID corresponding to a given URL."""
        if bugurl.startswith(ISSUE_URL):
            try:
                return int(bugurl[len(ISSUE_URL):])
            except ValueError:
                pass
        return None

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
    def login(cls, user: str, password: str) -> bool:
        """Login to bugzilla REST API."""
        session = Session()

        logger.info("Sending logging request...")
        params = {
            'login': user,
            'password': password,
        }

        fparams = urllib.parse.urlencode(params)
        req = f"{REST_BASE_URL}login?{fparams}"

        try:
            data = session.get(req, 0)
        except requests.exceptions.HTTPError:
            logger.error("Login failed")
            return False

        token = json.loads(data)['token']
        logger.info("Logging success")

        with TOKENFILE.open('w') as file:
            file.write(token)

        return True

    @classmethod
    def add_bug_comment(cls, bugid: int, comment: str):
        """Publish a new comment to a bugzilla issue."""
        with TOKENFILE.open('r') as file:
            token = file.read()

        data = {
            'token': token,
            'comment': comment,
        }

        url = f"{REST_BASE_URL}bug/{bugid}/comment"
        try:
            Session().post(url, data)
        except requests.exceptions.HTTPError:
            logging.error("Failed to post comment on Bugzilla, please login")
            raise
