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


class Session:
    """A session with persistent cookies."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not isinstance(cls._instance, cls):
            cls._instance = super().__new__(cls, *args, **kwargs)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if self._instance.initialized:
            return

        self.session = requests.Session()
        self.refresh_policy = RefreshPolicy.AUTO

        if COOKIESFILE.exists():
            with COOKIESFILE.open('rb') as file:
                self.session.cookies.update(pickle.load(file))

        self._instance.initialized = True

    def set_refresh_policy(self, policy: RefreshPolicy):
        """Set the global refresh policy."""
        self.refresh_policy = policy

    def refresh_policy_max_age(self, auto: int,
                               refresh_override: Optional[RefreshPolicy] = None
                               ) -> int:
        """Get the maximum age before refresh for a given policy."""
        policy = refresh_override if refresh_override else self.refresh_policy
        if policy == RefreshPolicy.FORCE:
            return 0
        if policy == RefreshPolicy.NO:
            return -1
        return auto

    def save_cookies(self):
        """Save cookies so they can be used for later sessions."""
        COOKIESFILE.parent.mkdir(parents=True, exist_ok=True)
        if _SESSION:
            with COOKIESFILE.open('wb') as file:
                pickle.dump(_SESSION.cookies, file)

    def invalidate_cache(self, url: str):
        """Invalidate cache for a given URL."""
        self._get_cache_file(url).unlink(missing_ok=True)

    def _get_cache_file(self, url: str) -> pathlib.Path:
        filestem = url.split('://', 1)[1].replace('/', '_').replace(':', '_')
        cachefile = utils.CACHEDIR / f"{filestem}.json"

        return cachefile

    def get(self, url: str, max_cache_age: int = -1) -> str:
        """Do a GET request."""
        cachefile = self._get_cache_file(url)
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

        logger.debug("Fetching %s, cache file will be %s", url, cachefile)
        req = self.session.get(url)
        req.raise_for_status()
        with cachefile.open('w') as file:
            file.write(req.text)

        return req.text

    def post(self, url: str, data: dict[str, Any]) -> str:
        """Do a POST request."""
        logger.debug("Sending POST request to %s with %s", url, data)
        req = self.session.post(url, data=data)

        req.raise_for_status()
        return req.text
