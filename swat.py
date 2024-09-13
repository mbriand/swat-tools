#!/usr/bin/env python3

import requests
import pickle
import pathlib
import click
import logging
import json
import tabulate

logger = logging.getLogger(__name__)

BINDIR = pathlib.Path(__file__).parent.resolve()
DATADIR = BINDIR / "data"
CACHEDIR = DATADIR / "cache"

COOKIESFILE = DATADIR / 'cookies'

BASE_URL = "https://swatbot.yoctoproject.org"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
REST_BASE_URL = f"{BASE_URL}/rest"

_SESSION = None


def get_session() -> requests.Session:
    global _SESSION
    if not _SESSION:
        _SESSION = requests.Session()

        with COOKIESFILE.open('rb') as f:
            _SESSION.cookies.update(pickle.load(f))

    return _SESSION


def get_json(path: str, refresh: bool = False):
    CACHEDIR.mkdir(parents=True, exist_ok=True)
    cachefile = CACHEDIR / f"{path.replace('/', '_')}.json"

    if cachefile.exists():
        logger.debug("Loading cache file for %s", path)
        with cachefile.open('r') as f:
            return json.load(f)

    logger.debug("Fetching %s", path)
    r = get_session().get(f"{REST_BASE_URL}{path}")
    r.raise_for_status()
    with cachefile.open('w') as f:
        f.write(r.text)

    return json.loads(r.text)


def get_build(buildid: int, refresh: bool = False):
    return get_json(f"/build/{buildid}/")['data']


def get_build_collection(collectionid: int, refresh: bool = False):
    return get_json(f"/buildcollection/{collectionid}/")['data']


def get_stepfailures(refresh: bool = False):
    logger.info("Loading build failures...")
    return get_json("/stepfailure/")['data']


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


def build_status_to_name(status: int) -> str:
    if status == 1:
        return "Warning"
    if status == 2:
        return "Error"
    return "Unknown"


@main.command()
@click.option('--limit', '-l', type=click.INT, default=0,
              help="Only list the n last entries")
@click.option('--sort', '-s', multiple=True, default=["Build"],
              help="Specify sort order")
def show_pending_failures_noowner(limit: int, sort: list[str]):
    failures = get_stepfailures()
    pending_ids = {f['relationships']['build']['data']['id'] for f in failures
                   if f['attributes']['triage'] == 0}

    infos = []
    for buildid in sorted(pending_ids, reverse=True):
        build = get_build(buildid)
        collectionid = build['relationships']['buildcollection']['data']['id']
        collection = get_build_collection(collectionid)

        if collection['attributes']['owner'] is not None:
            continue

        attributes = build['attributes']
        infos.append({'Build': attributes['buildid'],
                      'Status': build_status_to_name(attributes['status']),
                      'Test': attributes['targetname'],
                      'Worker': attributes['workername'],
                      'SWAT URL': f"{BASE_URL}/collection/{collection['id']}/",
                      'Autobuilder URL': attributes['url']
                      })

        if limit and len(infos) >= limit:
            break

    def sortfn(x):
        return tuple([x[k] for k in sort])

    headers = list(infos[0].keys())
    table = [info.values() for info in sorted(infos, key=sortfn)]

    print(tabulate.tabulate(table, headers=headers))


if __name__ == '__main__':
    main()
