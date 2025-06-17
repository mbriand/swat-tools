#!/usr/bin/env python3

"""Interaction with the swatbot Django server.

This module provides functionality for authenticating with and retrieving data
from the swatbot Django server via its REST API.
"""

import enum
import json
import logging
import urllib
from typing import Any, Optional

import requests

from . import utils
from .webrequests import Session

logger = logging.getLogger(__name__)

BASE_URL = "https://swatbot.yoctoproject.org"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
REST_BASE_URL = f"{BASE_URL}/rest"


class RefreshPolicy(enum.Enum):
    """A swatbot cache refresh policy.

    Defines how to handle cached data when making requests to the swatbot
    server.
    """

    NO = enum.auto()
    FORCE = enum.auto()
    AUTO = enum.auto()


class RefreshManager:
    """A refresh manager for the swatbot REST API.

    Singleton class that manages the refresh policy for API requests.
    """

    _instance = None

    AUTO_REFRESH_S = 60 * 60 * 24 * 30

    # pylint: disable=duplicate-code
    # pylint complains because of duplicate code in singleton init. We might do
    # better, but keep it that way for now.
    def __new__(cls, *args, **kwargs):
        if not isinstance(cls._instance, cls):
            cls._instance = super().__new__(cls, *args, **kwargs)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if self._instance.initialized:
            return

        self.refresh_policy = RefreshPolicy.AUTO

        self._instance.initialized = True

    def set_policy(self, policy: RefreshPolicy):
        """Set the global refresh policy.

        Args:
            policy: The refresh policy to set
        """
        self.refresh_policy = policy

    def set_policy_by_name(self, policy_name: str):
        """Set the global refresh policy from policy name.

        Args:
            policy_name: Name of the refresh policy (case-insensitive)
        """
        self.set_policy(RefreshPolicy[policy_name.upper()])

    def get_refresh_max_age(self,
                            refresh_override: Optional[RefreshPolicy] = None,
                            auto: int = AUTO_REFRESH_S
                            ) -> int:
        """Get the maximum age before refresh for a given policy.

        Args:
            refresh_override: Optional policy to override the global policy
            auto: Auto refresh interval in seconds

        Returns:
            Maximum age in seconds, 0 for force refresh, -1 for no refresh
        """
        policy = refresh_override if refresh_override else self.refresh_policy
        if policy == RefreshPolicy.FORCE:
            return 0
        if policy == RefreshPolicy.NO:
            return -1

        return auto


class TriageStatus(enum.IntEnum):
    """A status to set on a failure.

    Represents the different triage statuses that can be assigned to a failure.
    """

    @staticmethod
    def from_str(status: str) -> 'TriageStatus':
        """Get TriageStatus instance from its name as a string.

        Args:
            status: The name of the status (case-insensitive)

        Returns:
            The corresponding TriageStatus enum value
        """
        return TriageStatus[status.upper()]

    def __str__(self):
        return self.name.title()

    PENDING = 0
    MAIL_SENT = 1
    BUG = 2
    OTHER = 3
    NOT_FOR_SWAT = 4
    CANCELLED = 5


def _get_csrftoken() -> str:
    return Session().session.cookies['csrftoken']


def login(user: str, password: str) -> bool:
    """Login to the swatbot Django interface.

    Args:
        user: Username for authentication
        password: Password for authentication

    Returns:
        True if login was successful, False otherwise
    """
    session = Session()

    logger.info("Sending logging request...")
    session.get(LOGIN_URL)

    data = {
        "csrfmiddlewaretoken": _get_csrftoken(),
        "username": user,
        "password": password
    }

    try:
        session.post(LOGIN_URL, data=data)
    except requests.exceptions.HTTPError as error:
        if error.response.status_code != requests.codes.NOT_FOUND:
            raise error
    else:
        logger.warning("Unexpected reply, login probably failed")
        return False

    session.save_cookies()
    logger.info("Logging success")

    return True


def _get_json(path: str, max_cache_age: int = 0):
    url = f"{REST_BASE_URL}{path}"
    data = Session().get(url, max_cache_age != 0, max_cache_age)
    try:
        json_data = json.loads(data)
    except requests.exceptions.ConnectionError as err:
        raise utils.SwattoolException(f"Failed to fetch {url}") from err
    except json.decoder.JSONDecodeError as err:
        Session().invalidate_cache(f"{REST_BASE_URL}{path}")
        if "Please login to see this page." in data:
            raise utils.LoginRequiredException("Not logged in swatbot",
                                               "swatbot") from err
        raise utils.SwattoolException("Failed to parse server reply") from err
    return json_data


def get_build(buildid: int) -> dict:
    """Get info on a given build.

    Args:
        buildid: The ID of the build to retrieve
        refresh_override: Optional policy to override the global refresh policy

    Returns:
        Dictionary containing build information
    """
    return _get_json(f"/build/{buildid}/")['data']


def get_build_collection(collectionid: int) -> dict:
    """Get info on a given build collection.

    Args:
        collectionid: The ID of the collection to retrieve
        refresh_override: Optional policy to override the global refresh policy

    Returns:
        Dictionary containing collection information
    """
    return _get_json(f"/buildcollection/{collectionid}/")['data']


def invalidate_stepfailures_cache():
    """Invalidate cache for pending failures.

    This can be used to force fetching failures on next build, when we suspect
    it might have changed remotely.
    """
    Session().invalidate_cache(f"{REST_BASE_URL}/stepfailure/", allparams=True)


FAILURES_AUTO_REFRESH_S = 60 * 60 * 4
PENDING_FAILURES_AUTO_REFRESH_S = 60 * 10


def get_stepfailures(status: Optional[TriageStatus] = None,
                     refresh_override: Optional[RefreshPolicy] = None
                     ) -> list[dict]:
    """Get info on all failures.

    Args:
        status: Optional status to filter failures by
        refresh_override: Optional policy to override the global refresh policy

    Returns:
        List of failure data dictionaries
    """
    auto_refresh_s = FAILURES_AUTO_REFRESH_S
    params: dict[str, Any] = {}
    if status is not None:
        params['triage'] = status.value
        if status.value == TriageStatus.PENDING:
            auto_refresh_s = PENDING_FAILURES_AUTO_REFRESH_S

    request = f"/stepfailure/?{urllib.parse.urlencode(params)}"
    maxage = RefreshManager().get_refresh_max_age(refresh_override,
                                                  auto=auto_refresh_s)

    return _get_json(request, maxage)['data']


def get_stepfailure(failureid: int):
    """Get info on a given failure.

    Args:
        failureid: The ID of the failure to retrieve
        refresh_override: Optional policy to override the global refresh policy

    Returns:
        Dictionary containing failure information
    """
    return _get_json(f"/stepfailure/{failureid}/")['data']


def get_failures(status: Optional[TriageStatus] = None
                 ) -> dict[int, dict[int, dict]]:
    """Get all failures on swatbot server.

    Retrieves failures and organizes them by build ID and failure ID.

    Args:
        status: Optional status to filter failures by

    Returns:
        Dictionary mapping build IDs to dictionaries of failure IDs to failure
        data
    """
    failures = get_stepfailures(status)
    ids: dict[int, dict[int, dict]] = {}
    for failure_data in failures:
        buildid = int(failure_data['relationships']['build']['data']['id'])
        failureid = int(failure_data['id'])
        ids.setdefault(buildid, {})[failureid] = failure_data

    return ids


def publish_status(failureid: int,
                   status: TriageStatus, comment: str):
    """Publish new triage status to the swatbot Django server.

    Args:
        failureid: The ID of the failure to update
        status: The new triage status to set
        comment: Comment explaining the triage status
    """
    # Here we need to send a POST request to the page of any collection, there
    # is no need to use the page of the collection corresponding the failure we
    # want to update. Just use collection 1.
    swat_url = f"{BASE_URL}/collection/1/"

    data = {"csrfmiddlewaretoken": _get_csrftoken(),
            "failureid": failureid,
            "status": status.value,
            "notes": comment
            }
    Session().post(swat_url, data)
