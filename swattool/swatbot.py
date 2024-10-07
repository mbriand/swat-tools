#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import enum
import json
import logging
import pathlib
import shutil
from typing import Any, Collection, Optional

import click
import requests
import yaml

from . import swatbuild
from . import utils
from . import webrequests

logger = logging.getLogger(__name__)

USERINFOFILE = utils.DATADIR / "userinfos.yaml"

BASE_URL = "https://swatbot.yoctoproject.org"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
REST_BASE_URL = f"{BASE_URL}/rest"


class Status(enum.IntEnum):
    """The status of a failure."""

    WARNING = 1
    ERROR = 2
    CANCELLED = 6
    UNKNOWN = -1

    @staticmethod
    def from_int(status: int) -> 'Status':
        """Get Status instance from an integer status value."""
        try:
            return Status(status)
        except ValueError:
            return Status.UNKNOWN

    def __str__(self):
        return self.name.title()


class TriageStatus(enum.IntEnum):
    """A status to set on a failure."""

    PENDING = 0
    MAIL_SENT = 1
    BUG = 2
    OTHER = 3
    NOT_FOR_SWAT = 4
    CANCELLED = 5


FAILURES_AUTO_REFRESH_S = 60 * 60 * 4
AUTO_REFRESH_S = 60 * 60 * 24 * 30


def _get_csrftoken() -> str:
    session = webrequests.get_session()
    return session.cookies['csrftoken']


def login(user: str, password: str):
    """Login to the swatbot Django interface."""
    logger.info("Sending logging request...")
    webrequests.get(LOGIN_URL, 0)

    data = {
        "csrfmiddlewaretoken": _get_csrftoken(),
        "username": user,
        "password": password
    }

    try:
        webrequests.post(LOGIN_URL, data=data)
    except requests.exceptions.HTTPError as error:
        if error.response.status_code != requests.codes.NOT_FOUND:
            raise error
    else:
        logger.warning("Unexpected reply, login probably failed")
        return

    webrequests.save_cookies()
    logger.info("Logging success")


def _get_json(path: str, max_cache_age: int = -1):
    data = webrequests.get(f"{REST_BASE_URL}{path}", max_cache_age)
    try:
        json_data = json.loads(data)
    except json.decoder.JSONDecodeError:
        webrequests.invalidate_cache(f"{REST_BASE_URL}{path}")
        if "Please login to see this page." in data:
            raise utils.SwattoolException("Not logged in swatbot")
        raise utils.SwattoolException("Failed to parse server reply")
    return json_data


def get_build(buildid: int, refresh_override:
              Optional[webrequests.RefreshPolicy] = None):
    """Get info on a given build."""
    maxage = webrequests.refresh_policy_max_age(AUTO_REFRESH_S,
                                                refresh_override)
    return _get_json(f"/build/{buildid}/", maxage)['data']


def get_build_collection(collectionid: int, refresh_override:
                         Optional[webrequests.RefreshPolicy] = None):
    """Get info on a given build collection."""
    maxage = webrequests.refresh_policy_max_age(AUTO_REFRESH_S,
                                                refresh_override)
    return _get_json(f"/buildcollection/{collectionid}/", maxage)['data']


def invalidate_stepfailures_cache():
    """Invalidate cache for pending failures.

    This can be used to force fetching failures on next build, when we suspect
    it might have changed remotely.
    """
    webrequests.invalidate_cache(f"{REST_BASE_URL}/stepfailure/")


def get_stepfailures(refresh_override:
                     Optional[webrequests.RefreshPolicy] = None):
    """Get info on all failures."""
    maxage = webrequests.refresh_policy_max_age(FAILURES_AUTO_REFRESH_S,
                                                refresh_override)
    return _get_json("/stepfailure/", maxage)['data']


def get_stepfailure(failureid: int,
                    refresh_override:
                    Optional[webrequests.RefreshPolicy] = None):
    """Get info on a given failure."""
    maxage = webrequests.refresh_policy_max_age(FAILURES_AUTO_REFRESH_S,
                                                refresh_override)
    return _get_json(f"/stepfailure/{failureid}/", maxage)['data']


def _get_pending_failures() -> dict[int, dict[int, dict]]:
    failures = get_stepfailures()
    pending_ids: dict[int, dict[int, dict]] = {}
    for failure_data in failures:
        if failure_data['attributes']['triage'] == 0:
            buildid = int(failure_data['relationships']['build']['data']['id'])
            failureid = int(failure_data['id'])
            pending_ids.setdefault(buildid, {})[failureid] = failure_data

    return pending_ids


def get_user_infos() -> dict[int, dict[swatbuild.Field, Any]]:
    """Load user infos stored during previous review session."""
    logger.info("Loading saved data...")
    if USERINFOFILE.exists():
        with USERINFOFILE.open('r') as file:
            pretty_userinfos = yaml.load(file, Loader=yaml.Loader)
            userinfos = {bid: {swatbuild.Field(k): v for k, v in info.items()}
                         for bid, info in pretty_userinfos.items()}
            return userinfos
    return {}


def save_user_infos(userinfos: dict[int, dict[swatbuild.Field, Any]], suffix=""
                    ) -> pathlib.Path:
    """Store user infos for later runs."""
    pretty_userinfos = {bid: {str(k): v for k, v in info.items()}
                        for bid, info in userinfos.items() if info}

    filename = USERINFOFILE.with_stem(f'{USERINFOFILE.stem}{suffix}')
    with filename.open('w') as file:
        yaml.dump(pretty_userinfos, file)

    # Create backup files. We might remove this once the code becomes more
    # stable
    i = 0
    while filename.with_stem(f'{filename.stem}-backup-{i}').exists():
        i += 1
    shutil.copy(filename, filename.with_stem(f'{filename.stem}-backup-{i}'))

    return filename


def get_failure_infos(limit: int, sort: Collection[str],
                      filters: dict[str, Any]
                      ) -> tuple[list[swatbuild.Build],
                                 dict[int, dict[swatbuild.Field, Any]]]:
    """Get consolidated list of failure infos and local reviews infos."""
    userinfos = get_user_infos()

    logger.info("Loading build failures...")
    pending_failures = _get_pending_failures()

    # Generate a list of all pending failures, fetching details from the remote
    # server as needed.
    logger.info("Loading build failures details...")
    infos = []
    limited_pending_ids = sorted(pending_failures.keys(), reverse=True)[:limit]
    with click.progressbar(limited_pending_ids) as pending_ids_progress:
        for buildid in pending_ids_progress:
            build = swatbuild.Build(buildid, pending_failures[buildid])

            userinfo = userinfos.setdefault(build.id, {})
            if build.match_filters(filters, userinfo):
                infos.append(build)

    def sortfn(elem):
        return elem.get_sort_tuple([swatbuild.Field(k) for k in sort])

    return (sorted(infos, key=sortfn), userinfos)


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
    webrequests.post(swat_url, data)
