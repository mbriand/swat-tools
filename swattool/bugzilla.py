#!/usr/bin/env python3

"""Bugzilla related functions.

This module provides functionality for interacting with the Yocto Project Bugzilla
system, including retrieving bug information and posting comments.
"""

import urllib
import logging
import json
from typing import Optional

import requests
import tabulate

from .webrequests import Session
from . import utils

logger = logging.getLogger(__name__)

BASE_URL = "https://bugzilla.yoctoproject.org"
REST_BASE_URL = f"{BASE_URL}/rest/"
ISSUE_URL = f"{BASE_URL}/show_bug.cgi?id="

TOKENFILE = utils.DATADIR / 'bugzilla_token'


class Bug:
    """Bugzilla bug entry.

    Represents a bug from the Bugzilla system with its essential attributes.
    """

    # pylint: disable=too-few-public-methods

    def __init__(self, bugdata):
        self.id = bugdata['id']
        self.summary = bugdata['summary']
        self.classification = bugdata['classification']
        self.status = bugdata['status']
        self.resolution = bugdata['resolution']


class Bugzilla:
    """Bugzilla server interaction class.

    Provides static methods for interacting with the Bugzilla REST API.
    """

    CACHE_TIMEOUT_S = 60 * 60 * 24

    known_abints: dict[int, Bug] = {}

    @classmethod
    def get_abints(cls, force_refresh: bool = False) -> dict[int, Bug]:
        """Get a dictionary of all AB-INT issues.

        Retrieves bugs with AB-INT in their whiteboard from Bugzilla.

        Args:
            force_refresh: Whether to bypass the cache and force a refresh

        Returns:
            Dictionary mapping bug IDs to Bug objects
        """
        if not cls.known_abints or force_refresh:
            fields = ['summary', 'classification', 'status', 'resolution']
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
                'status_whiteboard': 'AB-INT',
                'status_whiteboard_type': 'allwordssubstr',
                'include_fields': ['id'] + fields,
            }

            fparams = urllib.parse.urlencode(params, doseq=True)
            req = f"{REST_BASE_URL}bug?{fparams}"
            cache_timeout = 0 if force_refresh else cls.CACHE_TIMEOUT_S

            try:
                data = Session().get(req, cache_timeout)
            except requests.exceptions.HTTPError:
                logger.error("Failed to get AB-INT list")
                return {}

            cls.known_abints = {bug['id']: Bug(bug)
                                for bug in json.loads(data)['bugs']}

        return cls.known_abints

    @classmethod
    def get_formatted_abints(cls, force_refresh: bool = False
                             ) -> list[str]:
        """Get a formatted list of all AB-INT issues.

        Retrieves AB-INT bugs and formats them as tabular text for display.

        Args:
            force_refresh: Whether to bypass the cache and force a refresh

        Returns:
            List of formatted strings representing bugs
        """

        def format_status(abint):
            if abint.status == "RESOLVED":
                return f"rslvd:{abint.resolution}".upper()
            return abint.status

        abints = cls.get_abints(force_refresh)
        table = [[abint.id, abint.summary, format_status(abint)]
                 for abint in abints.values()]
        return tabulate.tabulate(table, tablefmt="plain").splitlines()

    @classmethod
    def get_bug_url(cls, bugid: int) -> str:
        """Get the bugzilla URL corresponding to a given issue ID.

        Args:
            bugid: Bugzilla issue ID

        Returns:
            Full URL to the bug page
        """
        return f"{ISSUE_URL}{bugid}"

    @classmethod
    def get_bug_id_from_url(cls, bugurl: str) -> Optional[int]:
        """Get the bugzilla issue ID corresponding to a given URL.

        Args:
            bugurl: Bugzilla URL to extract ID from

        Returns:
            Bug ID as integer or None if URL is invalid
        """
        if bugurl.startswith(ISSUE_URL):
            try:
                return int(bugurl[len(ISSUE_URL):])
            except ValueError:
                pass
        return None

    @classmethod
    def get_bug_title(cls, bugid: int) -> Optional[str]:
        """Get bugzilla bug title.

        Retrieves the summary/title of a bug from Bugzilla.

        Args:
            bugid: Bugzilla issue ID

        Returns:
            Bug summary or None if not found
        """
        abints = cls.get_abints()
        if bugid in abints:
            return abints[bugid].summary

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
    def get_bug_description(cls, bugid: int) -> Optional[str]:
        """Get bugzilla bug description.

        Currently returns just the bug URL since fetching the actual description
        is too slow with the current Bugzilla version.

        Args:
            bugid: Bugzilla issue ID

        Returns:
            Bug URL as string
        """
        # It looks like this is too slow and the bugzilla version we are using
        # so far is not able to fetch this data at the same time as the ab ints
        # list. Next bugzilla releases will offer a 'description' field in bug
        # description.
        # Disable this and just show a link...
        # req = f"{REST_BASE_URL}bug/{bugid}/comment"
        # data = Session().get(req, cls.CACHE_TIMEOUT_S)

        # jsondata = json.loads(data)['bugs']
        # if str(bugid) not in jsondata:
        #     return None

        # comments = jsondata[str(bugid)]['comments']
        # if len(comments) < 1:
        #     return None

        # return comments[0]['text']
        return f'{BASE_URL}/show_bug.cgi?id={bugid}'

    @classmethod
    def login(cls, user: str, password: str) -> bool:
        """Login to bugzilla REST API.

        Authenticates with Bugzilla and stores the authentication token.

        Args:
            user: Bugzilla username
            password: Bugzilla password

        Returns:
            True if login was successful, False otherwise
        """
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
        """Publish a new comment to a bugzilla issue.

        Posts a new comment to a bug using the stored authentication token.

        Args:
            bugid: Bugzilla issue ID
            comment: Text content of the comment to add
        """
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
