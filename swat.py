#!/usr/bin/env python3

import requests
import pickle
import pathlib
import click
import logging
import json
import tabulate
import enum
import time
import subprocess
import shlex
from typing import Collection

logger = logging.getLogger(__name__)

BINDIR = pathlib.Path(__file__).parent.resolve()
DATADIR = BINDIR / "data"
CACHEDIR = DATADIR / "cache"

COOKIESFILE = DATADIR / 'cookies'

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
    STEPS = 'Steps'


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
    logger.info("Loading build failures...")
    maxage = refresh_policy_max_age(refresh, FAILURES_AUTO_REFRESH_S)
    return get_json("/stepfailure/", maxage)['data']


@click.group()
@click.option('-v', '--verbose', count=True, help="Increase verbosity")
def main(verbose: int):
    if verbose >= 1:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
    logging.basicConfig(level=loglevel)


@main.command()
@click.argument('user')
@click.argument('password')
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


@main.command()
@click.option('--limit', '-l', type=click.INT, default=0,
              help="Only parse the n last failures waiting for triage")
@click.option('--sort', '-s', multiple=True, default=["Build"],
              type=click.Choice([str(f) for f in Field],
                                case_sensitive=False),
              help="Specify sort order")
@click.option('--refresh', '-r',
              type=click.Choice([p.name for p in RefreshPolicy],
                                case_sensitive=False),
              default="auto",
              help="Fetch data from server instead of using cache")
@click.option('--test-filter', '-t', multiple=True,
              help="Only show some tests")
@click.option('--owner-filter', '-o', multiple=True,
              help='Only show some owners ("none" for no owner)')
@click.option('--ignore-test-filter', '-T', multiple=True,
              help="Ignore some tests")
@click.option('--status-filter', '-S', multiple=True,
              type=click.Choice([str(s) for s in Status],
                                case_sensitive=False),
              help="Only show some statuses")
@click.option('--open-url-with',
              help="Open the swatbot url with given program")
def show_pending_failures(limit: int, sort: Collection[str],
                          refresh: str,
                          test_filter: Collection[str],
                          ignore_test_filter: Collection[str],
                          status_filter: Collection[str],
                          owner_filter: Collection[str],
                          open_url_with: str):
    statusenum_filter = [Status[s.upper()] for s in status_filter]
    owners = [None if str(f).lower() == "none" else f for f in owner_filter]
    refreshpol = RefreshPolicy[refresh.upper()]
    failures = get_stepfailures(refresh=refreshpol)
    pending_ids: dict[str, list[str]] = {}
    for failure in failures:
        if failure['attributes']['triage'] == 0:
            buildid = failure['relationships']['build']['data']['id']
            stepname = failure['attributes']['stepname']
            pending_ids.setdefault(buildid, []).append(stepname)

    logger.info("Loading build failures details...")
    unique_pending_ids = sorted(pending_ids.keys(), reverse=True)[-limit:]
    infos = []
    with click.progressbar(unique_pending_ids) as pending_ids_progress:
        for buildid in pending_ids_progress:
            build = get_build(buildid, refresh=refreshpol)
            attributes = build['attributes']
            relationships = build['relationships']
            collectionid = relationships['buildcollection']['data']['id']
            collection = get_build_collection(collectionid, refresh=refreshpol)

            if owners and collection['attributes']['owner'] not in owners:
                continue

            if test_filter and attributes['targetname'] not in test_filter:
                continue

            if attributes['targetname'] in ignore_test_filter:
                continue

            status = Status.from_int(attributes['status'])
            if statusenum_filter and status not in statusenum_filter:
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
                          Field.STEPS: pending_ids[buildid]
                          })

            if open_url_with:
                subprocess.run(shlex.split(f"{open_url_with} {swat_url}"))

    def sortfn(x):
        return tuple([x[Field(k)] for k in sort])

    shown_fields = [
        Field.BUILD,
        Field.STATUS,
        Field.TEST,
        Field.OWNER,
        Field.WORKER,
        Field.COMPLETED,
        Field.SWAT_URL,
        # Field.AUTOBUILDER_URL,
        # Field.STEPS,
    ]
    headers = [str(f) for f in shown_fields]
    table = [[info[field] for field in shown_fields]
             for info in sorted(infos, key=sortfn)]

    print(tabulate.tabulate(table, headers=headers))

    logging.info("%s entries found (%s warnings and %s errors)", len(infos),
                 len([i for i in infos if i[Field.STATUS] == Status.ERROR]),
                 len([i for i in infos if i[Field.STATUS] == Status.WARNING]))


if __name__ == '__main__':
    main()
