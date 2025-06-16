#!/usr/bin/env python3

"""Swatbot log functions.

This module provides functionality for processing, analyzing, and highlighting
log files from Swatbot builds and failures.
"""

import gzip
import hashlib
import logging
import pickle
import re
from typing import Optional

import yaml

from . import swatbuild
from . import utils

logger = logging.getLogger(__name__)

HILIGHTS_FORMAT_VERSION = 3

# Big log thershold in lines
BIG_LOG_LIMIT = 1000 * 1000


class _Highlight:
    # pylint: disable=too-few-public-methods
    def __init__(self, keyword: str, color: str, in_menu: bool, text: str):
        self.keyword = keyword
        self.color = color
        self.in_menu = in_menu
        self.text = text


class _Filter:
    # pylint: disable=too-few-public-methods
    def __init__(self, pat: re.Pattern, enabled: bool, color: Optional[str],
                 in_menu: bool):
        self.pat = pat
        self.enabled = enabled
        self.color = color
        self.in_menu = in_menu

    def match(self, line: str) -> tuple[bool, Optional[_Highlight]]:
        """Check if the filter matches a given line.

        Args:
            line: The log line to check against the filter pattern

        Returns:
            A tuple containing a boolean indicating if the line matched and
            an optional highlight object.
        """
        if not self.enabled:
            return (False, None)

        match = self.pat.match(line)
        if not match:
            return (False, None)

        if not self.color:
            return (True, None)

        hilight = _Highlight(match.group("keyword"), self.color, self.in_menu,
                             line)
        return (True, hilight)


class Log:
    """Log handling class.

    Provides functionality to access, process, and highlight log files
    from build failures.
    """

    _cached_log_highlights: dict[tuple[swatbuild.Failure, str],
                                 dict[int, _Highlight]] = {}

    def __init__(self, failure: swatbuild.Failure, logname: str = 'stdio'):
        self.failure = failure
        self.logname = logname
        self._highlights = None

    def get_data(self):
        """Get logfile content.

        Returns:
            The content of the log file
        """
        return self.failure.get_log(self.logname)

    def _get_log_highlights_filters(self, loglen: int) -> list[_Filter]:
        status = self.failure.status
        test = self.failure.build.test

        if loglen > BIG_LOG_LIMIT:
            logging.warning("Log file for build %s (failure %s) is quite big: "
                            "using simplified log filters",
                            self.failure.build.id, self.failure.id)
            filters = [
                _Filter(re.compile(r"(?P<keyword>\S*error):", flags=re.I),
                        True, utils.Color.RED,
                        status == swatbuild.Status.ERROR),
                _Filter(re.compile(r"(?P<keyword>\S*warning):",
                                   flags=re.I),
                        True, utils.Color.YELLOW,
                        status == swatbuild.Status.WARNING),
            ]
        else:
            filters = [
                # Toaster specific rules:
                #  - Do nothing on "except xxxError:" (likely python code
                #    output).
                #  - Match on "selenium .*exception:".
                _Filter(re.compile(r".*except\s*\S*error:", flags=re.I),
                        test == "toaster", None, False),
                _Filter(re.compile(
                    r"(.*\s|^)(?P<keyword>selenium\.\S*exception):",
                    flags=re.I),
                    test == "toaster", utils.Color.RED,
                    status == swatbuild.Status.ERROR),

                # Generic rules:
                #  - Do nothing on "libgpg-error:".
                #  - Do nothing on "test_fixed_size_error:".
                #  - Do nothing on " error::.*ok" (tests cases).
                #  - Match on "error:", show in menu if build status is error.
                #  - Match on "warning:", show in menu if build status is
                #    warning.
                #  - Match on "fatal:", show in menu if build status is error.
                #  - Match on makefile "Error", show in menu if build status is
                #    error.
                #  - Match on makefile "command timed out", always show in
                #    menu.
                #  - Match on test failures (FAIL or FAILED), always show in
                #    menu.
                _Filter(re.compile(r".*libgpg-error:"), True, None, False),
                _Filter(re.compile(r".*test_fixed_size_error:"),
                        True, None, False),
                _Filter(re.compile(r".*( |::)error::.*ok"), True, None, False),
                _Filter(re.compile(r"(.*\s|^)(?P<keyword>\S*error):",
                                   flags=re.I),
                        True, utils.Color.RED,
                        status == swatbuild.Status.ERROR),
                _Filter(re.compile(r"(.*\s|^)(?P<keyword>\S*warning):",
                                   flags=re.I),
                        True, utils.Color.YELLOW,
                        status == swatbuild.Status.WARNING),
                _Filter(re.compile(r"^(?P<keyword>fatal):", flags=re.I),
                        True, utils.Color.RED,
                        status == swatbuild.Status.ERROR),
                _Filter(re.compile(
                    r"(.*\s|^)(?P<keyword>make\[\d\]):.* Error"),
                    True, utils.Color.RED,
                    status == swatbuild.Status.ERROR),
                _Filter(re.compile(r"^(?P<keyword>command timed out:)"),
                        True, utils.Color.RED, True),
                _Filter(re.compile(r".* - INFO -  ... (?P<keyword>FAIL)"),
                        True, utils.Color.RED, True),
                _Filter(re.compile(r"RESULTS - .*: (?P<keyword>FAILED)"),
                        True, utils.Color.RED, True),
            ]

        return filters

    @staticmethod
    def _build_log_highlights(loglines: list[str], filters: list[_Filter]
                              ) -> dict[int, _Highlight]:
        highlight_lines = {}
        for linenum, line in enumerate(loglines, start=1):
            for filtr in filters:
                matched, highlight = filtr.match(line)
                if matched:
                    if highlight:
                        highlight_lines[linenum] = highlight
                    break

        return highlight_lines

    def _load_cache_file(self, num_lines: int, filtershash: str
                         ) -> Optional[dict[int, _Highlight]]:
        cachedir = utils.CACHEDIR / 'log_hilights'
        cachefile = cachedir / f'{self.failure.id}_{self.logname}.yaml.gz'
        if not cachefile.is_file():
            return None

        with gzip.open(cachefile, mode='r') as file:
            try:
                data = yaml.load(file, Loader=yaml.Loader)
                if (data['version'] == HILIGHTS_FORMAT_VERSION
                        and data['numlines'] == num_lines
                        and data['filtershash'] == filtershash):
                    return data['hilights']
            except (TypeError, KeyError,
                    yaml.constructor.ConstructorError) as err:
                logging.warning("Failed to load highlights cache: %s", err)

        return None

    def _write_cache_file(self, num_lines: int, filtershash: str):
        cachedir = utils.CACHEDIR / 'log_hilights'
        cachefile = cachedir / f'{self.failure.id}_{self.logname}.yaml.gz'
        with gzip.open(cachefile, mode='w') as file:
            data = {
                'version': HILIGHTS_FORMAT_VERSION,
                'hilights': self._highlights,
                'numlines': num_lines,
                'filtershash': filtershash,
            }
            yaml.dump(data, file, encoding='utf-8')

    def _load_log_highlights(self):
        # Try to get data from memory cache
        cache_key = (self.failure, self.logname)
        self._highlights = self._cached_log_highlights.get(cache_key, None)
        if self._highlights:
            return

        logmetadata = self.failure.get_log_data(self.logname)
        if not logmetadata:
            self._highlights = {}
            return
        # Try to get data from disk cache
        filters = self._get_log_highlights_filters(logmetadata['num_lines'])
        filtershash = hashlib.sha256(pickle.dumps(filters))
        self._highlights = self._load_cache_file(logmetadata['num_lines'],
                                                 filtershash.hexdigest())

        # Generate hilights data
        if not self._highlights:
            logdata = self.failure.get_log(self.logname)
            if not logdata:
                self._highlights = {}
                return

            loglines = logdata.splitlines()
            self._highlights = self._build_log_highlights(loglines, filters)
            self._write_cache_file(logmetadata['num_lines'],
                                   filtershash.hexdigest())

        self._cached_log_highlights[cache_key] = self._highlights

    def get_highlights(self) -> dict[int, _Highlight]:
        """Get log highlights for a given log file.

        Loads or generates line-by-line highlights for the log file.

        Returns:
            Dictionary mapping line numbers to highlight objects
        """
        if self._highlights is None:
            self._load_log_highlights()

        assert self._highlights is not None
        return self._highlights

    def get_highlights_text(self) -> list[str]:
        """Get highlighted text lines from a log file.

        Returns:
            List of highlighted text lines
        """
        highlights = self.get_highlights()
        return [highlights[line].text for line in sorted(highlights)
                if highlights[line].in_menu]
