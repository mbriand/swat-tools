#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import enum
import json
import logging
from typing import Optional

import requests

from . import utils
from .webrequests import RefreshPolicy, Session

logger = logging.getLogger(__name__)

BASE_URL = "https://swatbot.yoctoproject.org"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
REST_BASE_URL = f"{BASE_URL}/rest"


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


FAILURES_AUTO_REFRESH_S = 60 * 60 * 4
AUTO_REFRESH_S = 60 * 60 * 24 * 30


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
    maxage = Session().refresh_policy_max_age(AUTO_REFRESH_S, refresh_override)
    return _get_json(f"/build/{buildid}/", maxage)['data']


def get_build_collection(collectionid: int, refresh_override:
                         Optional[RefreshPolicy] = None):
    """Get info on a given build collection."""
    maxage = Session().refresh_policy_max_age(AUTO_REFRESH_S,
                                              refresh_override)
    return _get_json(f"/buildcollection/{collectionid}/", maxage)['data']


def invalidate_stepfailures_cache():
    """Invalidate cache for pending failures.

    This can be used to force fetching failures on next build, when we suspect
    it might have changed remotely.
    """
    Session().invalidate_cache(f"{REST_BASE_URL}/stepfailure/")


def get_stepfailures(refresh_override: Optional[RefreshPolicy] = None):
    """Get info on all failures."""
    maxage = Session().refresh_policy_max_age(FAILURES_AUTO_REFRESH_S,
                                              refresh_override)
    return _get_json("/stepfailure/", maxage)['data']


def get_stepfailure(failureid: int,
                    refresh_override: Optional[RefreshPolicy] = None):
    """Get info on a given failure."""
    maxage = Session().refresh_policy_max_age(FAILURES_AUTO_REFRESH_S,
                                              refresh_override)
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
