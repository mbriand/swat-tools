#!/usr/bin/env python3

"""Interaction with the swatbot Django server."""

import enum
import json
import logging
from typing import Optional

import requests

from . import utils
from .webrequests import Session

logger = logging.getLogger(__name__)

BASE_URL = "https://swatbot.yoctoproject.org"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
REST_BASE_URL = f"{BASE_URL}/rest"


class RefreshPolicy(enum.Enum):
    """A swatbot cache refresh policy."""

    NO = enum.auto()
    FORCE = enum.auto()
    FORCE_FAILURES = enum.auto()
    AUTO = enum.auto()


class RefreshManager:
    """A refresh manager for the swatbot REST API."""

    _instance = None

    FAILURES_AUTO_REFRESH_S = 60 * 60 * 4
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
        """Set the global refresh policy."""
        self.refresh_policy = policy

    def set_policy_by_name(self, policy_name: str):
        """Set the global refresh policy from policy name."""
        self.set_policy(RefreshPolicy[policy_name.upper()])

    def get_refresh_max_age(self,
                            refresh_override: Optional[RefreshPolicy] = None,
                            failures: bool = False
                            ) -> int:
        """Get the maximum age before refresh for a given policy."""
        policy = refresh_override if refresh_override else self.refresh_policy
        if policy == RefreshPolicy.FORCE_FAILURES:
            policy = RefreshPolicy.FORCE if failures else RefreshPolicy.AUTO

        if policy == RefreshPolicy.FORCE:
            return 0
        if policy == RefreshPolicy.NO:
            return -1

        if failures:
            return self.FAILURES_AUTO_REFRESH_S
        return self.AUTO_REFRESH_S


class TriageStatus(enum.IntEnum):
    """A status to set on a failure."""

    @staticmethod
    def from_str(status: str) -> 'TriageStatus':
        """Get TriageStatus instance from its name as a string."""
        return TriageStatus[status.upper()]

    PENDING = 0
    MAIL_SENT = 1
    BUG = 2
    OTHER = 3
    NOT_FOR_SWAT = 4
    CANCELLED = 5


def _get_csrftoken() -> str:
    return Session().session.cookies['csrftoken']


def login(user: str, password: str):
    """Login to the swatbot Django interface."""
    session = Session()

    logger.info("Sending logging request...")
    session.get(LOGIN_URL, 0)

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
        return

    session.save_cookies()
    logger.info("Logging success")


def _get_json(path: str, max_cache_age: int = -1):
    data = Session().get(f"{REST_BASE_URL}{path}", max_cache_age)
    try:
        json_data = json.loads(data)
    except json.decoder.JSONDecodeError as err:
        Session().invalidate_cache(f"{REST_BASE_URL}{path}")
        if "Please login to see this page." in data:
            raise utils.SwattoolException("Not logged in swatbot") from err
        raise utils.SwattoolException("Failed to parse server reply") from err
    return json_data


def get_build(buildid: int, refresh_override: Optional[RefreshPolicy] = None):
    """Get info on a given build."""
    maxage = RefreshManager().get_refresh_max_age(refresh_override)
    return _get_json(f"/build/{buildid}/", maxage)['data']


def get_build_collection(collectionid: int, refresh_override:
                         Optional[RefreshPolicy] = None):
    """Get info on a given build collection."""
    maxage = RefreshManager().get_refresh_max_age(refresh_override)
    return _get_json(f"/buildcollection/{collectionid}/", maxage)['data']


def invalidate_stepfailures_cache():
    """Invalidate cache for pending failures.

    This can be used to force fetching failures on next build, when we suspect
    it might have changed remotely.
    """
    Session().invalidate_cache(f"{REST_BASE_URL}/stepfailure/")


def get_stepfailures(refresh_override: Optional[RefreshPolicy] = None):
    """Get info on all failures."""
    maxage = RefreshManager().get_refresh_max_age(refresh_override,
                                                     failures=True)
    return _get_json("/stepfailure/", maxage)['data']


def get_stepfailure(failureid: int,
                    refresh_override: Optional[RefreshPolicy] = None):
    """Get info on a given failure."""
    maxage = RefreshManager().get_refresh_max_age(refresh_override)
    return _get_json(f"/stepfailure/{failureid}/", maxage)['data']


def get_pending_failures() -> dict[int, dict[int, dict]]:
    """Get all pending failures on swatbot server."""
    failures = get_stepfailures()
    pending_ids: dict[int, dict[int, dict]] = {}
    for failure_data in failures:
        if failure_data['attributes']['triage'] == 0:
            buildid = int(failure_data['relationships']['build']['data']['id'])
            failureid = int(failure_data['id'])
            pending_ids.setdefault(buildid, {})[failureid] = failure_data

    return pending_ids


def publish_status(failureid: int,
                   status: TriageStatus, comment: str):
    """Publish new triage status to the swatbot Django server."""
    # TODO: remove and publish result using REST API
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
