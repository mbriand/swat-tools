#!/usr/bin/env python3

"""Wrapper for requests module with cookies persistence and basic cache."""

import gzip
import hashlib
import logging
import pathlib
import pickle
import time
import threading
import zlib
from typing import Any, Optional

import requests

from . import utils

logger = logging.getLogger(__name__)

COOKIESFILE = utils.DATADIR / 'cookies'

cache_lock = threading.Lock()


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

        if COOKIESFILE.exists():
            with COOKIESFILE.open('rb') as file:
                self.session.cookies.update(pickle.load(file))

        self._instance.initialized = True

    def save_cookies(self):
        """Save cookies so they can be used for later sessions."""
        COOKIESFILE.parent.mkdir(parents=True, exist_ok=True)
        if self.session:
            with COOKIESFILE.open('wb') as file:
                pickle.dump(self.session.cookies, file)

    def invalidate_cache(self, url: str):
        """Invalidate cache for a given URL."""
        with cache_lock:
            for file in self._get_cache_file_candidates(url):
                file.unlink(missing_ok=True)

    def _get_cache_file_candidates(self, url: str) -> list[pathlib.Path]:
        filestem = url.split('://', 1)[1].replace('/', '_').replace(':', '_')

        if len(filestem) > 100:
            hashname = hashlib.sha256(filestem.encode(), usedforsecurity=False)
            filestem = hashname.hexdigest()

        candidates = [
            utils.CACHEDIR / f"{filestem}.gz",
            utils.CACHEDIR / filestem,

            # For compatibility with old cache files
            utils.CACHEDIR / f"{filestem}.json",
        ]

        return candidates

    def _try_load_cache(self, cachefile: pathlib.Path, max_cache_age: int
                        ) -> Optional[str]:
        if max_cache_age < 0:
            use_cache = True
        else:
            age = time.time() - cachefile.stat().st_mtime
            use_cache = age < max_cache_age

        if use_cache:
            if cachefile.suffix == ".gz":
                try:
                    with gzip.open(cachefile, mode='r') as gzfile:
                        return gzfile.read(-1).decode()
                except zlib.error:
                    logging.warning("Failed to read %s cache file, ignoring",
                                    cachefile)
            else:
                with cachefile.open('r') as file:
                    return file.read(-1)

        return None

    def _create_cache_file(self, cachefile: pathlib.Path, data: str):
        if cachefile.suffix == ".gz":
            with gzip.open(cachefile, mode='w') as gzfile:
                gzfile.write(data.encode())
        else:
            with cachefile.open('w') as file:
                file.write(data)

    def get(self, url: str, max_cache_age: int = -1) -> str:
        """Do a GET request."""
        cache_candidates = self._get_cache_file_candidates(url)
        cache_new_file = cache_candidates[0]
        cache_new_file.parent.mkdir(parents=True, exist_ok=True)

        with cache_lock:
            cache_olds = [file for file in cache_candidates if file.exists()]
            for cachefile in cache_olds:
                data = self._try_load_cache(cachefile, max_cache_age)
                if data:
                    logger.debug("Loaded cache file for %s: %s", url,
                                 cachefile)
                    return data

        logger.debug("Fetching %s, cache file will be %s", url, cache_new_file)
        req = self.session.get(url)
        req.raise_for_status()

        with cache_lock:
            for cachefile in cache_olds:
                cachefile.unlink()
            self._create_cache_file(cache_new_file, req.text)

        return req.text

    def post(self, url: str, data: dict[str, Any]) -> str:
        """Do a POST request."""
        logger.debug("Sending POST request to %s with %s", url, data)
        req = self.session.post(url, data=data)

        req.raise_for_status()
        return req.text
