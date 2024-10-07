#!/usr/bin/env python3

"""Wrapper for requests module with cookies persistence and basic cache."""

import enum
import logging
import pathlib
import pickle
import time
from typing import Any, Optional

import requests

from . import utils

logger = logging.getLogger(__name__)

COOKIESFILE = utils.DATADIR / 'cookies'

_SESSION = None


class RefreshPolicy(enum.Enum):
    """A cache refresh policy."""

    NO = enum.auto()
    FORCE = enum.auto()
    AUTO = enum.auto()


_REFRESH_POLICY = RefreshPolicy.AUTO


def refresh_policy_max_age(auto: int,
                           refresh_override: Optional[RefreshPolicy] = None
                           ) -> int:
    """Get the maximum age before refresh for a given policy."""
    policy = refresh_override if refresh_override else _REFRESH_POLICY
    if policy == RefreshPolicy.FORCE:
        return 0
    if policy == RefreshPolicy.NO:
        return -1
    return auto


def set_refresh_policy(policy: RefreshPolicy):
    """Set the global refresh policy."""
    global _REFRESH_POLICY
    _REFRESH_POLICY = policy


def get_session() -> requests.Session:
    """Get the underlying requests object."""
    global _SESSION
    if not _SESSION:
        _SESSION = requests.Session()

        if COOKIESFILE.exists():
            with COOKIESFILE.open('rb') as file:
                _SESSION.cookies.update(pickle.load(file))

    return _SESSION


def save_cookies():
    """Save cookies so they can be used for later sessions."""
    COOKIESFILE.parent.mkdir(parents=True, exist_ok=True)
    if _SESSION:
        with COOKIESFILE.open('wb') as file:
            pickle.dump(_SESSION.cookies, file)


def invalidate_cache(url: str):
    """Invalidate cache for a given URL."""
    _get_cache_file(url).unlink(missing_ok=True)


def _get_cache_file(url: str) -> pathlib.Path:
    filestem = url.split('://', 1)[1].replace('/', '_').replace(':', '_')
    cachefile = utils.CACHEDIR / f"{filestem}.json"

    return cachefile


def get(url: str, max_cache_age: int = -1) -> str:
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
            with cachefile.open('r') as file:
                return file.read(-1)

    logger.debug("Fetching %s", url)
    req = get_session().get(url)
    req.raise_for_status()
    with cachefile.open('w') as file:
        file.write(req.text)

    return req.text


def post(url: str, data: dict[str, Any]) -> str:
    """Do a POST request."""
    logger.debug("Sending POST request to %s with %s", url, data)
    req = get_session().post(url, data=data)

    req.raise_for_status()
    return req.text
