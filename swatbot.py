#!/usr/bin/env python3

import requests
import pickle
import pathlib
import click
import logging
import json
import enum
import time
import yaml
import shutil
import re
from datetime import datetime
from typing import Any, Collection

logger = logging.getLogger(__name__)

BINDIR = pathlib.Path(__file__).parent.resolve()
DATADIR = BINDIR / "data"
CACHEDIR = DATADIR / "cache"

COOKIESFILE = DATADIR / 'cookies'
USERINFOFILE = DATADIR / "userinfos.yaml"

BASE_URL = "https://swatbot.yoctoproject.org"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
REST_BASE_URL = f"{BASE_URL}/rest"

_SESSION = None


class RefreshPolicy(enum.Enum):
    NO = enum.auto()
    FORCE = enum.auto()
    AUTO = enum.auto()


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


def refresh_policy_max_age(policy: RefreshPolicy, auto: int) -> int:
    if policy == RefreshPolicy.FORCE:
        return 0
    if policy == RefreshPolicy.NO:
        return -1
    return auto


FAILURES_AUTO_REFRESH_S = 60 * 60 * 4
AUTO_REFRESH_S = 60 * 60 * 24 * 7


def get_session() -> requests.Session:
    global _SESSION
    if not _SESSION:
        _SESSION = requests.Session()

        with COOKIESFILE.open('rb') as f:
            _SESSION.cookies.update(pickle.load(f))

    return _SESSION


def get_json(path: str, max_cache_age: int = -1):
    CACHEDIR.mkdir(parents=True, exist_ok=True)
    cachefile = CACHEDIR / f"{path.replace('/', '_')}.json"

    if cachefile.exists():
        if max_cache_age < 0:
            use_cache = True
        else:
            age = time.time() - cachefile.stat().st_mtime
            use_cache = age < max_cache_age

        if use_cache:
            logger.debug("Loading cache file for %s", path)
            with cachefile.open('r') as f:
                return json.load(f)

    logger.debug("Fetching %s", path)
    r = get_session().get(f"{REST_BASE_URL}{path}")
    r.raise_for_status()
    with cachefile.open('w') as f:
        f.write(r.text)

    return json.loads(r.text)


def get_build(buildid: int, refresh: RefreshPolicy = RefreshPolicy.NO):
    maxage = refresh_policy_max_age(refresh, AUTO_REFRESH_S)
    return get_json(f"/build/{buildid}/", maxage)['data']


def get_build_collection(collectionid: int,
                         refresh: RefreshPolicy = RefreshPolicy.NO):
    maxage = refresh_policy_max_age(refresh, AUTO_REFRESH_S)
    return get_json(f"/buildcollection/{collectionid}/", maxage)['data']


def get_stepfailures(refresh: RefreshPolicy = RefreshPolicy.NO):
    maxage = refresh_policy_max_age(refresh, FAILURES_AUTO_REFRESH_S)
    return get_json("/stepfailure/", maxage)['data']


def login(user: str, password: str):
    logger.info("Sending logging request...")
    session = requests.Session()
    r = session.get(LOGIN_URL)
    r.raise_for_status()

    data = {
        "csrfmiddlewaretoken": session.cookies['csrftoken'],
        "username": user,
        "password": password
    }
    r = session.post(LOGIN_URL, data=data)

    if r.status_code not in [requests.codes.ok, requests.codes.not_found]:
        r.raise_for_status()

    COOKIESFILE.parent.mkdir(parents=True, exist_ok=True)
    with COOKIESFILE.open('wb') as f:
        pickle.dump(session.cookies, f)
    logger.info("Logging success")


def get_user_infos() -> dict[int, dict[Field, Any]]:
    logger.info("Loading saved data...")
    if USERINFOFILE.exists():
        with USERINFOFILE.open('r') as f:
            pretty_userinfos = yaml.load(f, Loader=yaml.Loader)
            userinfos = {bid: {Field(k): v for k, v in info.items()}
                         for bid, info in pretty_userinfos.items()}
            return userinfos
    return {}


def get_failure_infos(limit: int, sort: Collection[str],
                      refresh: str,
                      test_filter: Collection[str],
                      ignore_test_filter: Collection[str],
                      status_filter: Collection[str],
                      owner_filter: Collection[str],
                      completed_after: datetime,
                      with_notes: bool
                      ) -> tuple[list[dict[Field, Any]],
                                 dict[int, dict[Field, Any]]]:
    statusenum_filter = [Status[s.upper()] for s in status_filter]
    owners = [None if str(f).lower() == "none" else f for f in owner_filter]
    refreshpol = RefreshPolicy[refresh.upper()]

    if completed_after:
        completed_after = completed_after.astimezone()

    userinfos = get_user_infos()

    logger.info("Loading build failures...")
    failures = get_stepfailures(refresh=refreshpol)
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

    logger.info("Loading build failures details...")
    infos = []
    limited_pending_ids = sorted(pending_ids.keys(), reverse=True)[:limit]
    with click.progressbar(limited_pending_ids) as pending_ids_progress:
        for buildid in pending_ids_progress:
            build = get_build(buildid, refresh=refreshpol)
            attributes = build['attributes']
            relationships = build['relationships']
            collectionid = relationships['buildcollection']['data']['id']
            collection = get_build_collection(collectionid, refresh=refreshpol)

            if owners and collection['attributes']['owner'] not in owners:
                continue

            matches = [True for f in test_filter
                       if re.match(f"^{f}$", attributes['targetname'])]
            if test_filter and not matches:
                continue

            if attributes['targetname'] in ignore_test_filter:
                continue

            status = Status.from_int(attributes['status'])
            if statusenum_filter and status not in statusenum_filter:
                continue

            if completed_after and attributes['completed']:
                completed = datetime.fromisoformat(attributes['completed'])
                if completed < completed_after:
                    continue

            if with_notes:
                userinfo = userinfos.setdefault(attributes['buildid'], {})
                if not userinfo.get(Field.USER_NOTES):
                    continue

            # Keys must be in TABLE_HEADER
            swat_url = f"{BASE_URL}/collection/{collection['id']}/"
            infos.append({Field.BUILD: attributes['buildid'],
                          Field.STATUS: status,
                          Field.TEST: attributes['targetname'],
                          Field.WORKER: attributes['workername'],
                          Field.COMPLETED: attributes['completed'],
                          Field.SWAT_URL: swat_url,
                          Field.AUTOBUILDER_URL: attributes['url'],
                          Field.OWNER: collection['attributes']['owner'],
                          Field.FAILURES: pending_ids[buildid],
                          })

    def sortfn(x):
        return tuple([x[Field(k)] for k in sort])

    return (sorted(infos, key=sortfn), userinfos)


def save_user_infos(userinfos: dict[int, dict[Field, Any]]):
    pretty_userinfos = {bid: {str(k): v for k, v in info.items()}
                        for bid, info in userinfos.items()}

    with USERINFOFILE.open('w') as f:
        yaml.dump(pretty_userinfos, f)

    i = 0
    while USERINFOFILE.with_stem(f'{USERINFOFILE.stem}-backup-{i}').exists():
        i += 1
    shutil.copy(USERINFOFILE,
                USERINFOFILE.with_stem(f'{USERINFOFILE.stem}-backup-{i}'))
