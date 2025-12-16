#!/usr/bin/env python3

"""Wrapper for requests module with cookies persistence and basic cache.

This module provides a session manager that maintains cookies across
requests and implements a file-based cache for responses.
"""

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

COOKIESFILE = utils.DATADIR / "cookies"
MIN_HASHED_FILESTEM = 50

cache_lock = threading.Lock()


class Session:
    """A session with persistent cookies.

    Singleton class that wraps requests.Session with cookie persistence
    and response caching capabilities.
    """

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
        adapter = requests.adapters.HTTPAdapter(
            pool_maxsize=20, pool_block=True
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        if COOKIESFILE.exists():
            with COOKIESFILE.open("rb") as file:
                self.session.cookies.update(pickle.load(file))

        self._instance.initialized = True

    def save_cookies(self):
        """Save cookies so they can be used for later sessions.

        Writes the current session cookies to a file for persistence.
        """
        COOKIESFILE.parent.mkdir(parents=True, exist_ok=True)
        if self.session:
            with COOKIESFILE.open("wb") as file:
                pickle.dump(self.session.cookies, file)

    def get_cookies(self) -> dict:
        """Get session cookies as a dictionary.

        Returns:
            Dictionary of cookies from the current session
        """
        return dict(self.session.cookies) if self.session else {}

    def invalidate_cache(self, url: str, allparams: bool = False):
        """Invalidate cache for a given URL.

        Args:
            url: The URL to invalidate cache for
            allparams: If True, invalidate all parameter variations of the URL
        """
        with cache_lock:
            for file in self._get_cache_file_candidates(url):
                file.unlink(missing_ok=True)
            if allparams:
                prefix = self._get_cache_file_prefix(url)
                for file in prefix.parent.glob(f"{prefix.name}\\?*"):
                    file.unlink()
                if prefix.name.endswith("_"):
                    subdir = prefix.parent / f"{prefix.name[:-1]}/"
                    for file in subdir.glob("[?]*"):
                        file.unlink()

    def _get_old_cache_file_prefix(self, url: str) -> pathlib.Path:
        filestem = url.split("://", 1)[1].replace("/", "_").replace(":", "_")

        if len(filestem) > MIN_HASHED_FILESTEM:
            hashname = hashlib.sha256(filestem.encode(), usedforsecurity=False)
            filestem = hashname.hexdigest()

        return utils.CACHEDIR / filestem

    def _get_cache_file_prefix(self, url: str) -> pathlib.Path:
        if url.endswith("/"):
            url = url[:-1] + "_"
        filename = pathlib.Path(url.split("://", 1)[1].replace(":", "_"))

        if len(filename.stem) > MIN_HASHED_FILESTEM:
            hashname = hashlib.sha256(
                filename.stem.encode(), usedforsecurity=False
            )
            filename = filename.with_stem(hashname.hexdigest())

        return utils.CACHEDIR / filename

    def _get_cache_file_candidates(self, url: str) -> list[pathlib.Path]:
        prefix = self._get_cache_file_prefix(url)
        old_prefix = self._get_old_cache_file_prefix(url)

        candidates = [
            prefix.parent / f"{prefix.name}.gz",
            prefix,
            # For compatibility with old cache files
            old_prefix.parent / f"{old_prefix.name}.gz",
            old_prefix,
            old_prefix.parent / f"{old_prefix.name}.json",
        ]

        return candidates

    def _try_load_cache(
        self, cachefile: pathlib.Path, max_cache_age: int
    ) -> Optional[str]:
        if max_cache_age < 0:
            use_cache = True
        else:
            age = time.time() - cachefile.stat().st_mtime
            use_cache = age < max_cache_age

        if use_cache:
            if cachefile.suffix == ".gz":
                try:
                    with gzip.open(cachefile, mode="r") as gzfile:
                        return gzfile.read(-1).decode()
                except zlib.error:
                    logging.warning(
                        "Failed to read %s cache file, ignoring", cachefile
                    )
            else:
                with cachefile.open("r") as file:
                    return file.read(-1)

        return None

    def _create_cache_file(self, cachefile: pathlib.Path, data: str):
        cachefile.parent.mkdir(parents=True, exist_ok=True)
        if cachefile.suffix == ".gz":
            with gzip.open(cachefile, mode="w") as gzfile:
                gzfile.write(data.encode())
        else:
            with cachefile.open("w") as file:
                file.write(data)

    def get(
        self,
        url: str,
        cache_store: bool = False,
        max_cache_age: int = 0,
        headers=None,
    ) -> str:
        """Do a GET request.

        Attempts to load response from cache if available and not expired,
        otherwise performs a real request and caches the result.

        Args:
            url: The URL to request
            cache_store: Whether to store the response in cache
            max_cache_age: Maximum age in seconds for cached responses,
                          0 for no cache, -1 for unlimited
            headers: Optional headers to include in the request

        Returns:
            Response text content

        Raises:
            requests.exceptions.HTTPError: If the request fails
        """
        if cache_store or max_cache_age != 0:
            cache_candidates = self._get_cache_file_candidates(url)
            cache_new_file = cache_candidates[0]

            with cache_lock:
                cache_olds = [
                    file for file in cache_candidates if file.is_file()
                ]
                for cachefile in cache_olds:
                    data = self._try_load_cache(cachefile, max_cache_age)
                    if data:
                        logger.debug(
                            "Loaded cache file for %s: %s", url, cachefile
                        )
                        return data

            logger.debug(
                "Fetching %s, cache file will be %s", url, cache_new_file
            )
        else:
            logger.debug("Fetching %s", url)

        req = self.session.get(url, headers=headers)
        req.raise_for_status()

        if cache_store:
            with cache_lock:
                cache_olds = [
                    file for file in cache_candidates if file.is_file()
                ]
                for cachefile in cache_olds:
                    cachefile.unlink()
                self._create_cache_file(cache_new_file, req.text)

        return req.text

    def post(
        self,
        url: str,
        data: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        headers=None,
    ) -> str:
        """Do a POST request.

        Args:
            url: The URL to post to
            data: Optional dictionary of form data to send in the request
            json: Optional dictionary of JSON data to send in the request
            headers: Optional headers to include in the request

        Returns:
            Response text content

        Raises:
            requests.exceptions.HTTPError: If the request fails
        """
        logger.debug("Sending POST request to %s with %s", url, data)
        req = self.session.post(url, data=data, json=json, headers=headers)

        req.raise_for_status()

        return req.text

    def put(
        self,
        url: str,
        data: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
        headers=None,
    ) -> str:
        """Do a PUT request.

        Args:
            url: The URL to send the PUT request to
            data: Optional dictionary of form data to send in the request
            json: Optional dictionary of JSON data to send in the request
            headers: Optional headers to include in the request

        Returns:
            Response text content

        Raises:
            requests.exceptions.HTTPError: If the request fails
        """
        logger.debug("Sending PUT request to %s with %s", url, data)
        req = self.session.put(url, data=data, json=json, headers=headers)

        req.raise_for_status()
        return req.text
