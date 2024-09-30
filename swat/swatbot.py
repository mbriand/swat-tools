#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import click
import enum
import json
import logging
import pathlib
import requests
import shutil
import tabulate
import textwrap
import yaml
from datetime import datetime
from typing import Any, Collection

from . import bugzilla
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


class Field(enum.StrEnum):
    """A filed in failure info."""

    BUILD = 'Build'
    STATUS = 'Status'
    TEST = 'Test'
    OWNER = 'Owner'
    WORKER = 'Worker'
    COMPLETED = 'Completed'
    SWAT_URL = 'SWAT URL'
    AUTOBUILDER_URL = 'Autobuilder URL'
    FAILURES = 'Failures'
    USER_NOTES = 'Notes'
    USER_STATUS = 'Triage'


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
    except requests.exceptions.HTTPError as e:
        if e.response.status_code != requests.codes.NOT_FOUND:
            raise e
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
            raise Exception("Not logged in swatbot")
        else:
            raise Exception("Failed to parse server reply")
    return json_data


def get_build(buildid: int,
              refresh: webrequests.RefreshPolicy =
              webrequests.RefreshPolicy.AUTO
              ):
    """Get info on a given build."""
    maxage = webrequests.refresh_policy_max_age(refresh, AUTO_REFRESH_S)
    return _get_json(f"/build/{buildid}/", maxage)['data']


def get_build_collection(collectionid: int,
                         refresh: webrequests.RefreshPolicy =
                         webrequests.RefreshPolicy.AUTO):
    """Get info on a given build collection."""
    maxage = webrequests.refresh_policy_max_age(refresh, AUTO_REFRESH_S)
    return _get_json(f"/buildcollection/{collectionid}/", maxage)['data']


def invalidate_stepfailures_cache():
    """Invalidate cache for pending failures.

    This can be used to force fetching failures on next build, when we suspect
    it might have changed remotely.
    """
    webrequests.invalidate_cache(f"{REST_BASE_URL}/stepfailure/")


def get_stepfailures(refresh: webrequests.RefreshPolicy =
                     webrequests.RefreshPolicy.AUTO):
    """Get info on all failures."""
    maxage = webrequests.refresh_policy_max_age(refresh,
                                                FAILURES_AUTO_REFRESH_S)
    return _get_json("/stepfailure/", maxage)['data']


def get_stepfailure(failureid: int, refresh: webrequests.RefreshPolicy =
                    webrequests.RefreshPolicy.AUTO):
    """Get info on a given failure."""
    maxage = webrequests.refresh_policy_max_age(refresh,
                                                FAILURES_AUTO_REFRESH_S)
    return _get_json(f"/stepfailure/{failureid}/", maxage)['data']


def get_pending_failures(refresh: webrequests.RefreshPolicy
                         ) -> dict[int, dict[int, dict[str, Any]]]:
    """Get info on all pending failures."""
    failures = get_stepfailures(refresh=refresh)
    pending_ids: dict[int, dict[int, dict[str, Any]]] = {}
    for failure in failures:
        if failure['attributes']['triage'] == 0:
            buildid = int(failure['relationships']['build']['data']['id'])
            failureid = int(failure['id'])
            urls = {u.split()[0].rsplit('/')[-1]: u
                    for u in failure['attributes']['urls'].split()}
            faildata = {'stepname': failure['attributes']['stepname'],
                        'urls': urls}
            pending_ids.setdefault(buildid, {})[failureid] = faildata

    return pending_ids


def get_user_infos() -> dict[int, dict[Field, Any]]:
    """Load user infos stored during previous review session."""
    logger.info("Loading saved data...")
    if USERINFOFILE.exists():
        with USERINFOFILE.open('r') as f:
            pretty_userinfos = yaml.load(f, Loader=yaml.Loader)
            userinfos = {bid: {Field(k): v for k, v in info.items()}
                         for bid, info in pretty_userinfos.items()}
            return userinfos
    return {}


def save_user_infos(userinfos: dict[int, dict[Field, Any]], suffix=""
                    ) -> pathlib.Path:
    """Store user infos for later runs."""
    pretty_userinfos = {bid: {str(k): v for k, v in info.items()}
                        for bid, info in userinfos.items() if info}

    filename = USERINFOFILE.with_stem(f'{USERINFOFILE.stem}{suffix}')
    with filename.open('w') as f:
        yaml.dump(pretty_userinfos, f)

    # Create backup files. We might remove this once the code becomes more
    # stable
    i = 0
    while filename.with_stem(f'{filename.stem}-backup-{i}').exists():
        i += 1
    shutil.copy(filename, filename.with_stem(f'{filename.stem}-backup-{i}'))

    return filename


def _info_match_filters(info: dict[Field, Any],
                        userinfo: dict[Field, Any],
                        filters: dict[str, Any]
                        ) -> bool:
    if filters['build'] and info[Field.BUILD] not in filters['build']:
        return False

    if filters['owner'] and info[Field.OWNER] not in filters['owner']:
        return False

    matches = [True for r in filters['test'] if r.match(info[Field.TEST])]
    if filters['test'] and not matches:
        return False

    matches = [True for r in filters['ignore-test']
               if r.match(info[Field.TEST])]
    if filters['ignore-test'] and matches:
        return False

    status = Status.from_int(info[Field.STATUS])
    if filters['status'] and status not in filters['status']:
        return False

    if filters['completed-after'] and info[Field.COMPLETED]:
        completed = datetime.fromisoformat(info[Field.COMPLETED])
        if completed < filters['completed-after']:
            return False

    if filters['with-notes'] is not None:
        if filters['with-notes'] ^ bool(userinfo.get(Field.USER_NOTES)):
            return False

    if filters['with-new-status'] is not None:
        if filters['with-new-status'] ^ bool(userinfo.get(Field.USER_STATUS)):
            return False

    return True


def get_failure_infos(limit: int, sort: Collection[str],
                      refresh: webrequests.RefreshPolicy,
                      filters: dict[str, Any]
                      ) -> tuple[list[dict[Field, Any]],
                                 dict[int, dict[Field, Any]]]:
    """Get consolidated list of failure infos and local reviews infos."""
    userinfos = get_user_infos()

    logger.info("Loading build failures...")
    pending_ids = get_pending_failures(refresh)

    # Generate a list of all pending failures, fetching details from the remote
    # server as needed.
    logger.info("Loading build failures details...")
    infos = []
    limited_pending_ids = sorted(pending_ids.keys(), reverse=True)[:limit]
    with click.progressbar(limited_pending_ids) as pending_ids_progress:
        for buildid in pending_ids_progress:
            build = get_build(buildid, refresh=refresh)
            attributes = build['attributes']
            relationships = build['relationships']
            collectionid = relationships['buildcollection']['data']['id']
            collection = get_build_collection(collectionid, refresh=refresh)
            status = Status.from_int(attributes['status'])

            userinfo = userinfos.setdefault(attributes['buildid'], {})
            swat_url = f"{BASE_URL}/collection/{collection['id']}/"

            info = {Field.BUILD: attributes['buildid'],
                    Field.STATUS: status,
                    Field.TEST: attributes['targetname'],
                    Field.WORKER: attributes['workername'],
                    Field.COMPLETED: attributes['completed'],
                    Field.SWAT_URL: swat_url,
                    Field.AUTOBUILDER_URL: attributes['url'],
                    Field.OWNER: collection['attributes']['owner'],
                    Field.FAILURES: pending_ids[buildid],
                    }

            if _info_match_filters(info, userinfo, filters):
                infos.append(info)

    # Sort all failures as requested.
    def get_field(info, field):
        if field in info:
            return info[field]
        if field in userinfos[info[Field.BUILD]]:
            return userinfos[info[Field.BUILD]][field]
        return None

    def sortfn(x):
        return tuple([get_field(x, Field(k)) for k in sort])

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


def get_failure_description(info: dict[Field, Any],
                            userinfo: dict[Field, Any]) -> str:
    """Get info on one given failure in a pretty way."""
    abints = bugzilla.get_abints()

    simple_fields = [
        Field.BUILD,
        Field.STATUS,
        Field.TEST,
        Field.OWNER,
        Field.WORKER,
        Field.COMPLETED,
        Field.SWAT_URL,
        Field.AUTOBUILDER_URL,
    ]
    table = [[k, info[k]] for k in simple_fields]

    statuses = userinfo.get(Field.USER_STATUS, [])
    failures = info[Field.FAILURES]
    for i, (failureid, failure) in enumerate(failures.items()):
        status_str = ""

        # Create strings for all failures and the attributed new status (if one
        # was set).
        for status in statuses:
            if failureid in status['failures']:
                statusfrags = []

                statusname = status['status'].name.title()
                statusfrags.append(f"{statusname}: {status['comment']}")

                if status['status'] == TriageStatus.BUG:
                    bugid = int(status['comment'])
                    if bugid in abints:
                        bugtitle = abints[bugid]
                        statusfrags.append(f", {bugtitle}")

                if status.get('bugzilla-comment'):
                    statusfrags.append("\n")
                    bcom = [textwrap.fill(line)
                            for line in status['bugzilla-comment'].split('\n')]
                    statusfrags.append("\n".join(bcom))

                status_str += "".join(statusfrags)

                break
        table.append([Field.FAILURES if i == 0 else "",
                      failure['stepname'], status_str])

    desc = tabulate.tabulate(table, tablefmt="plain")

    usernotes = userinfo.get(Field.USER_NOTES)
    if usernotes:
        # Reserve chars for spacing.
        reserved = 8
        termwidth = shutil.get_terminal_size((80, 20)).columns
        width = termwidth - reserved
        wrapped = textwrap.indent(textwrap.fill(usernotes, width), " " * 4)
        desc += f"\n\n{Field.USER_NOTES}:\n{wrapped}"

    return desc
