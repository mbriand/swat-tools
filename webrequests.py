#!/usr/bin/env python3

import requests
import pickle
import logging
import enum
import time
import utils

logger = logging.getLogger(__name__)

COOKIESFILE = utils.DATADIR / 'cookies'

_SESSION = None


class RefreshPolicy(enum.Enum):
    NO = enum.auto()
    FORCE = enum.auto()
    AUTO = enum.auto()


def refresh_policy_max_age(policy: RefreshPolicy, auto: int) -> int:
    if policy == RefreshPolicy.FORCE:
        return 0
    if policy == RefreshPolicy.NO:
        return -1
    return auto


def get_session() -> requests.Session:
    global _SESSION
    if not _SESSION:
        _SESSION = requests.Session()

        with COOKIESFILE.open('rb') as f:
            _SESSION.cookies.update(pickle.load(f))

    return _SESSION


def save_cookies(session: requests.Session):
    COOKIESFILE.parent.mkdir(parents=True, exist_ok=True)
    with COOKIESFILE.open('wb') as f:
        pickle.dump(session.cookies, f)


def get(url: str, max_cache_age: int = -1):
    utils.CACHEDIR.mkdir(parents=True, exist_ok=True)
    filestem = url.split('://', 1)[1].replace('/', '_').replace(':', '_')
    cachefile = utils.CACHEDIR / f"{filestem}.json"

    if cachefile.exists():
        if max_cache_age < 0:
            use_cache = True
        else:
            age = time.time() - cachefile.stat().st_mtime
            use_cache = age < max_cache_age

        if use_cache:
            logger.debug("Loading cache file for %s", url)
            with cachefile.open('r') as f:
                return f.read(-1)

    logger.debug("Fetching %s", url)
    r = get_session().get(url)
    r.raise_for_status()
    with cachefile.open('w') as f:
        f.write(r.text)

    return r.text
