#!/usr/bin/env python3

import click
import enum
import json
import logging
import pathlib
import shutil
import utils
import yaml
from datetime import datetime
from typing import Any, Collection

import webrequests

logger = logging.getLogger(__name__)

USERINFOFILE = utils.DATADIR / "userinfos.yaml"

BASE_URL = "https://swatbot.yoctoproject.org"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
REST_BASE_URL = f"{BASE_URL}/rest"


class Status(enum.IntEnum):
    WARNING = 1
    ERROR = 2
    UNKNOWN = -1

    @staticmethod
    def from_int(status: int) -> 'Status':
        try:
            return Status(status)
        except ValueError:
            return Status.UNKNOWN

    def __str__(self):
        return self.name.title()


class Field(enum.StrEnum):
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
    USER_STATUS = 'New triage status'


class TriageStatus(enum.IntEnum):
    PENDING = 0
    MAIL_SENT = 1
    BUG = 2
    OTHER = 3
    NOT_FOR_SWAT = 4
    CANCELLED = 5


FAILURES_AUTO_REFRESH_S = 60 * 60 * 4
AUTO_REFRESH_S = 60 * 60 * 24 * 30


def get_json(path: str, max_cache_age: int = -1):
    data = webrequests.get(f"{REST_BASE_URL}{path}", max_cache_age)
    return json.loads(data)


def get_build(buildid: int,
              refresh: webrequests.RefreshPolicy =
              webrequests.RefreshPolicy.AUTO
              ):
    maxage = webrequests.refresh_policy_max_age(refresh, AUTO_REFRESH_S)
    return get_json(f"/build/{buildid}/", maxage)['data']


def get_build_collection(collectionid: int,
                         refresh: webrequests.RefreshPolicy =
                         webrequests.RefreshPolicy.AUTO):
    maxage = webrequests.refresh_policy_max_age(refresh, AUTO_REFRESH_S)
    return get_json(f"/buildcollection/{collectionid}/", maxage)['data']


def get_stepfailures(refresh: webrequests.RefreshPolicy =
                     webrequests.RefreshPolicy.AUTO):
    maxage = webrequests.refresh_policy_max_age(refresh,
                                                FAILURES_AUTO_REFRESH_S)
    return get_json("/stepfailure/", maxage)['data']


def invalidate_stepfailures_cache():
    webrequests.invalidate_cache(f"{REST_BASE_URL}/stepfailure/")


def get_stepfailure(failureid: int, refresh: webrequests.RefreshPolicy =
                    webrequests.RefreshPolicy.AUTO):
    maxage = webrequests.refresh_policy_max_age(refresh,
                                                FAILURES_AUTO_REFRESH_S)
    return get_json(f"/stepfailure/{failureid}/", maxage)['data']


def _get_csrftoken() -> str:
    session = webrequests.get_session()
    return session.cookies['csrftoken']


def login(user: str, password: str):
    logger.info("Sending logging request...")
    webrequests.get(LOGIN_URL, 0)

    data = {
        "csrfmiddlewaretoken": _get_csrftoken(),
        "username": user,
        "password": password
    }
    webrequests.post(LOGIN_URL, data=data)

    webrequests.save_cookies()
    logger.info("Logging success")


def get_pending_failures(refresh: webrequests.RefreshPolicy
                         ) -> dict[int, dict[int, dict[str, Any]]]:
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
    userinfos = get_user_infos()

    logger.info("Loading build failures...")
    pending_ids = get_pending_failures(refresh)

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
                   failuredata,  # TODO: remove
                   status: TriageStatus, comment: str):
    # TODO: remove and publish result using REST API
    # TODO: try to post on an url without collection ID ?
    failure = get_stepfailure(failureid, refresh=webrequests.RefreshPolicy.NO)
    buildid = failure['relationships']['build']['data']['id']
    build = get_build(buildid, refresh=webrequests.RefreshPolicy.NO)
    buildcollection = build['relationships']['buildcollection']
    colid = buildcollection['data']['id']
    swat_url = f"{BASE_URL}/collection/{colid}/"

    data = {"csrfmiddlewaretoken": _get_csrftoken(),
            "failureid": failureid,
            "status": status.value,
            "notes": comment
            }
    webrequests.post(swat_url, data)
