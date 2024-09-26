#!/usr/bin/env python3

"""Wrapper for requests module with cookies persistence and basic cache."""

import enum
import logging
import pathlib
import pickle
import requests
import time
from typing import Any

from . import utils

logger = logging.getLogger(__name__)

COOKIESFILE = utils.DATADIR / 'cookies'

_SESSION = None


class RefreshPolicy(enum.Enum):
    """A cache refresh policy."""

    NO = enum.auto()
    FORCE = enum.auto()
    AUTO = enum.auto()


def refresh_policy_max_age(policy: RefreshPolicy, auto: int) -> int:
    """Get the maximum age before refresh for a given policy."""
    if policy == RefreshPolicy.FORCE:
        return 0
    if policy == RefreshPolicy.NO:
        return -1
    return auto


def get_session() -> requests.Session:
    """Get the underlying requests object."""
    global _SESSION
    if not _SESSION:
        _SESSION = requests.Session()

        with COOKIESFILE.open('rb') as f:
            _SESSION.cookies.update(pickle.load(f))

    return _SESSION


def save_cookies():
    """Save cookies so they can be used for later sessions."""
    COOKIESFILE.parent.mkdir(parents=True, exist_ok=True)
    if _SESSION:
        with COOKIESFILE.open('wb') as f:
            pickle.dump(_SESSION.cookies, f)


def invalidate_cache(url: str):
    """Invalidate cache for a given URL."""
    _get_cache_file(url).unlink(missing_ok=True)


def _get_cache_file(url: str) -> pathlib.Path:
    filestem = url.split('://', 1)[1].replace('/', '_').replace(':', '_')
    cachefile = utils.CACHEDIR / f"{filestem}.json"

    return cachefile


def get(url: str, max_cache_age: int = -1):
    """Do a GET request."""
    cachefile = _get_cache_file(url)
    cachefile.parent.mkdir(parents=True, exist_ok=True)

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


def post(url: str, data: dict[str, Any]):
    """Do a POST request."""
    logger.debug("Sending POST request to %s with %s", url, data)
    r = get_session().post(url, data=data)

    if not r.ok:
        print(r.text)
    r.raise_for_status()
