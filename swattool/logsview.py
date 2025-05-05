#!/usr/bin/env python3

"""Swatbot review functions."""

import gzip
import hashlib
import logging
import pickle
import re
import shutil
from typing import Optional

import jellyfish
from simple_term_menu import TerminalMenu  # type: ignore
import yaml

from . import swatbuild
from . import utils

logger = logging.getLogger(__name__)

HILIGHTS_FORMAT_VERSION = 2

# Big log thershold in bytes
BIG_LOG_LIMIT = 100 * 1024 * 1024


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
        """Check if the filter matches a given line."""
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


class LogFingerprint:
    """A logfile fingerprint, allowing to compute similarity with others."""

    _similarity_scores: dict[tuple[tuple[int, str], ...], float] = {}
    threshold = .7

    def __init__(self, failure: swatbuild.Failure, logname: str):
        self.failure = failure
        self.logname = logname

        # Limit finger print to the 100 first highlights. This is way above the
        # number of hilights for most log files but allow to handle rare cases
        # with thousands of matches.
        self.lines = get_log_highlights(failure, logname)[:100]

    def _get_similarity_score(self, other: 'LogFingerprint') -> float:
        """Get similarity score between log of this entry and another log."""
        if not self.lines or not other.lines:
            return 1 if not self.lines and not other.lines else 0

        specific_error_re = re.compile(r"^\S+error:",
                                       flags=re.IGNORECASE | re.MULTILINE)

        # Compute scores for all fingerprint fragment combinations
        # Only consider combinations with similar positions in the files:
        # reduce both false positives and computation time.
        scores = [[0 for f2 in other.lines] for f1 in self.lines]
        lendiff = len(self.lines) - len(other.lines)
        for i, fing1 in enumerate(self.lines):
            for j, fing2 in enumerate(other.lines):
                maxdist = 2
                startdist = i - j
                enddist = lendiff - startdist
                if min(abs(startdist), abs(enddist)) > maxdist:
                    continue
                scores[i][j] = jellyfish.jaro_similarity(fing1, fing2)

        # Compute the final score as 2 half-scores: fingerprint A to B, then B
        # to A, so the similarity score is commutative.
        def half_score(fingerprint, hafnum):
            num = 0
            denom = 0
            for i, fragment in enumerate(fingerprint):
                # Lines with a specific error, such as "AssertionError" and not
                # just "ERROR:" are more likely to be decisive: reflect this in
                # the similarity score.
                factor = 5 if any(specific_error_re.finditer(fragment)) else 1

                if hafnum == 0:
                    bestsim = max(scores[i])
                else:
                    bestsim = max(s[i] for s in scores)
                num += factor * bestsim if bestsim > .7 else 0
                denom += factor

            score = num / denom
            return score

        s1 = half_score(self.lines, 0)
        s2 = half_score(other.lines, 1)

        return (s1 + s2) / 2

    def _get_cached_score(self,
                          failure: Optional[swatbuild.Failure] = None,
                          logname: Optional[str] = None,
                          other: Optional['LogFingerprint'] = None,
                          ) -> float:
        if failure is None or logname is None:
            assert failure is None and logname is None
            assert other is not None
            failure = other.failure
            logname = other.logname
        else:
            assert other is None
        key = tuple(sorted(((self.failure.id, self.logname),
                            (failure.id, logname))))
        score = self._similarity_scores.get(key)
        if score is None:
            if other is None:
                other = LogFingerprint(failure, logname)
            score = self._get_similarity_score(other)
            self._similarity_scores[key] = score

        return score

    def get_similarity_score(self, other: 'LogFingerprint') -> float:
        """Get similarity score between log of this entry and another log."""
        return self._get_cached_score(other=other)

    def get_similarity_score_with_failure(self, failure: swatbuild.Failure,
                                          logname: str) -> float:
        """Get similarity score between log of this entry and another log."""
        return self._get_cached_score(failure, logname)

    def is_similar_to(self, other: 'LogFingerprint') -> bool:
        """Check if a given log fingerprint is similar to this one."""
        return self._get_cached_score(other=other) > self.threshold

    def is_similar_to_failure(self, failure: swatbuild.Failure, logname: str
                              ) -> bool:
        """Check if a given log fingerprint is similar to this one."""
        return self._get_cached_score(failure, logname) > self.threshold


def show_logs_menu(build: swatbuild.Build) -> bool:
    """Show a menu allowing to select log file to analyze."""
    def get_failure_line(failure, logname):
        return (failure.id, failure.stepnumber, failure.stepname, logname)
    logs = [(failure, logname)
            for failure in build.failures.values()
            for logname in failure.urls]
    entries = [get_failure_line(failure, logname) for failure, logname in logs]
    default_line = get_failure_line(build.get_first_failure(), 'stdio')
    entry = entries.index(default_line)
    logs_menu = utils.tabulated_menu(entries, title="Log files",
                                     cursor_index=entry)

    while True:
        newentry = logs_menu.show()
        if newentry is None:
            break

        show_log_menu(*logs[newentry])

    return True


def _format_log_line(linenum: int, text: str, colorized_line: Optional[int],
                     highlight_lines: dict[int, _Highlight]):
    if linenum == colorized_line:
        if linenum in highlight_lines:
            linecolor = highlight_lines[linenum].color
        else:
            linecolor = utils.Color.CYAN
        text = utils.Color.colorize(text, linecolor)
    elif linenum in highlight_lines:
        pat = highlight_lines[linenum].keyword
        color = highlight_lines[linenum].color
        text = text.replace(pat, utils.Color.colorize(pat, color))
    return text


def _split_preview_line(text: str, preview_width: int):
    preview_text = text.expandtabs(4)
    preview_width -= 1 + 6 + 1  # space + line number + space
    return [preview_text[offset:offset + preview_width]
            for offset in range(0, max(1, len(preview_text)), preview_width)]


def _escape_log_line(text):
    return repr(text)[1:-1]


def _format_log_preview_line(linenum: int, text: str, colorized_line: int,
                             highlight_lines: dict[int, _Highlight],
                             preview_width: int):
    text = _escape_log_line(text)
    for i, wrappedtext in enumerate(_split_preview_line(text, preview_width)):
        formatted_text = _format_log_line(linenum, wrappedtext, colorized_line,
                                          highlight_lines)
        if i == 0:
            yield f"{linenum: 6d} {formatted_text}"
        else:
            yield f"{' ' * 6} {formatted_text}"


def _get_preview_window(linenum: int, lines: list[str], preview_height: int,
                        preview_width: int) -> tuple[int, int]:
    # All values below are in line index in the lines list, not line numbers.
    lineidx = linenum - 1

    # Place the start on given line and rewind until we have desired height
    # before our line.
    start = lineidx
    before_len = 0
    target_before_len = int(preview_height / 3)
    while start > 0 and before_len < target_before_len:
        nextline = start - 1
        linecount = len(_split_preview_line(lines[nextline], preview_width))
        if before_len + linecount > target_before_len:
            break

        start = nextline
        before_len += linecount

    # Place the end on given line and add lines until we reach full height.
    end = lineidx
    total_len = before_len
    total_len += len(_split_preview_line(lines[end], preview_width))
    while end < len(lines) - 1 and total_len < preview_height:
        end += 1
        total_len += len(_split_preview_line(lines[end], preview_width))

    # Special case on end of buffer: add some lines before.
    while start > 0 and total_len < preview_height:
        nextline = start - 1
        linecount = len(_split_preview_line(lines[nextline], preview_width))
        if total_len + linecount > preview_height:
            break

        start = nextline
        total_len += linecount

    return (start, end)


def _format_log_preview(linenum: int, lines: list[str],
                        highlight_lines: dict[int, _Highlight],
                        preview_height: int, preview_width: int) -> str:
    start, end = _get_preview_window(linenum, lines, preview_height,
                                     preview_width)
    lines = [previewline
             for i, t in enumerate(lines[start: end + 1], start=start + 1)
             for previewline in _format_log_preview_line(i, t, linenum,
                                                         highlight_lines,
                                                         preview_width)
             ]
    return "\n".join(lines[:preview_height])


def _get_log_highlights_filters(loglen: int, failure: swatbuild.Failure
                                ) -> list[_Filter]:
    status = failure.status
    test = failure.build.test

    if loglen > BIG_LOG_LIMIT:
        logging.warning("Log file for build %s (failure %s) is quite big: "
                        "using simplified log filters",
                        failure.build.id, failure.id)
        filters = [
            _Filter(re.compile(r"(?P<keyword>\S*error):", flags=re.I),
                    True, utils.Color.RED, status == swatbuild.Status.ERROR),
            _Filter(re.compile(r"(?P<keyword>\S*warning):",
                               flags=re.I),
                    True, utils.Color.YELLOW,
                    status == swatbuild.Status.WARNING),
        ]
    else:
        filters = [
            # Toaster specific rules:
            #  - Do nothing on "except xxxError:" (likely python code output).
            #  - Match on "selenium .*exception:".
            _Filter(re.compile(r".*except\s*\S*error:", flags=re.I),
                    test == "toaster", None, False),
            _Filter(re.compile(r"(.*\s|^)(?P<keyword>selenium\.\S*exception):",
                               flags=re.I),
                    test == "toaster", utils.Color.RED,
                    status == swatbuild.Status.ERROR),

            # Generic rules:
            #  - Do nothing on "libgpg-error:".
            #  - Do nothing on "test_fixed_size_error:".
            #  - Do nothing on " error::.*ok" (tests cases).
            #  - Match on "error:", show in menu if build status is error.
            #  - Match on "warning:", show in menu if build status is warning.
            #  - Match on "fatal:", show in menu if build status is error.
            #  - Match on makefile "Error", show in menu if build status is
            #    error.
            #  - Match on makefile "command timed out", always show in menu.
            _Filter(re.compile(r".*libgpg-error:"), True, None, False),
            _Filter(re.compile(r".*test_fixed_size_error:"),
                    True, None, False),
            _Filter(re.compile(r".*( |::)error::.*ok"), True, None, False),
            _Filter(re.compile(r"(.*\s|^)(?P<keyword>\S*error):", flags=re.I),
                    True, utils.Color.RED, status == swatbuild.Status.ERROR),
            _Filter(re.compile(r"(.*\s|^)(?P<keyword>\S*warning):",
                               flags=re.I),
                    True, utils.Color.YELLOW,
                    status == swatbuild.Status.WARNING),
            _Filter(re.compile(r"^(?P<keyword>fatal):", flags=re.I),
                    True, utils.Color.RED, status == swatbuild.Status.ERROR),
            _Filter(re.compile(r"(.*\s|^)(?P<keyword>make\[\d\]):.* Error"),
                    True, utils.Color.RED, status == swatbuild.Status.ERROR),
            _Filter(re.compile(r"^(?P<keyword>command timed out:)"),
                    True, utils.Color.RED, True),
            _Filter(re.compile(r".* - INFO -  ... (?P<keyword>FAIL)"),
                    True, utils.Color.RED, True),
        ]

    return filters


def _get_log_highlights(loglines: list[str], filters: list[_Filter]
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


_cached_log_highlights: dict[tuple[swatbuild.Failure, str],
                             dict[int, _Highlight]] = {}
_cached_log_fingerprint: dict[tuple[swatbuild.Failure, str],
                              LogFingerprint] = {}


def _get_cached_log_highlights(failure: swatbuild.Failure, logname: str,
                               ) -> dict[int, _Highlight]:
    # Try to get data from memory cache
    highlights = _cached_log_highlights.get((failure, logname), None)
    if highlights:
        return highlights

    logdata = failure.get_log(logname)
    if not logdata:
        return {}

    # Try to get data from disk cache
    cachedir = utils.CACHEDIR / 'log_hilights'
    cachefile = cachedir / f'{failure.id}_{logname}.yaml.gz'
    loghash = hashlib.sha256(logdata.encode())
    filters = _get_log_highlights_filters(len(logdata), failure)
    filtershash = hashlib.sha256(pickle.dumps(filters))
    if cachefile.is_file():
        with gzip.open(cachefile, mode='r') as file:
            try:
                data = yaml.load(file, Loader=yaml.Loader)
                if (data['version'] == HILIGHTS_FORMAT_VERSION
                        and data['sha256'] == loghash.hexdigest()
                        and data['filtershash'] == filtershash.hexdigest()):
                    highlights = data['hilights']
            except (TypeError, KeyError):
                pass

    # Generate hilights data
    if not highlights:
        loglines = logdata.splitlines()
        highlights = _get_log_highlights(loglines, filters)
        with gzip.open(cachefile, mode='w') as file:
            data = {
                'version': HILIGHTS_FORMAT_VERSION,
                'hilights': highlights,
                'sha256': loghash.hexdigest(),
                'filtershash': filtershash.hexdigest(),
            }
            yaml.dump(data, file, encoding='utf-8')

    _cached_log_highlights[(failure, logname)] = highlights

    return highlights


def get_log_highlights(failure: swatbuild.Failure, logname: str
                       ) -> list[str]:
    """Get log highlights for a given log file."""
    highlights = _get_cached_log_highlights(failure, logname)

    return [highlight.text for highlight in highlights.values()
            if highlight.in_menu]


def get_log_fingerprint(failure: swatbuild.Failure,
                        logname: str) -> LogFingerprint:
    """Get a finger print of the log, allowing to compare it with others."""
    fingerprint = _cached_log_fingerprint.get((failure, logname), None)
    if fingerprint is not None:
        return fingerprint

    fingerprint = LogFingerprint(failure, logname)
    _cached_log_fingerprint[(failure, logname)] = fingerprint

    return fingerprint


def _show_log(loglines: list[str], selected_line: Optional[int],
              highlight_lines: dict[int, _Highlight],
              preview_height: Optional[int], preview_width: Optional[int]):
    colorlines = [_format_log_line(i, t, selected_line, highlight_lines)
                  for i, t in enumerate(loglines, start=1)]

    startline: Optional[int]
    if selected_line and preview_height and preview_width:
        startline, _ = _get_preview_window(selected_line, loglines,
                                           preview_height, preview_width)
        startline += 1  # Use line number, not line index
    else:
        startline = selected_line
    utils.show_in_less("\n".join(colorlines), startline)


def _get_preview_sizes(preview_size: float) -> tuple[int, int]:
    termsize = shutil.get_terminal_size((80, 20))
    preview_height = int(preview_size * termsize.lines)
    preview_width = termsize.columns - 2  # Borders

    return (preview_height, preview_width)


def show_log_menu(failure: swatbuild.Failure, logname: str) -> bool:
    """Analyze a failure log file."""
    logdata = failure.get_log(logname)
    if not logdata:
        return False

    utils.clear()
    loglines = logdata.splitlines()
    highlights = _get_cached_log_highlights(failure, logname)

    entries = ["View entire log file|",
               "View entire log file in default editor|",
               *[f"On line {line: 6d}: {highlights[line].keyword}|{line}"
                 for line in sorted(highlights)
                 if highlights[line].in_menu]
               ]

    preview_size = 0.6
    preview_height, preview_width = _get_preview_sizes(preview_size)

    def preview(line):
        return _format_log_preview(int(line), loglines, highlights,
                                   preview_height, preview_width)

    title = f"{failure.build.format_short_description()}: " \
            f"{logname} of step {failure.stepnumber}"
    entry = 2
    while True:
        menu = TerminalMenu(entries, title=title, cursor_index=entry,
                            preview_command=preview, preview_size=preview_size,
                            raise_error_on_interrupt=True)
        entry = menu.show()
        if entry is None:
            return True

        if entry == 0:
            _show_log(loglines, None, highlights, None, None)
        elif entry == 1:
            utils.launch_in_system_defaultshow_in_less(logdata)
        else:
            _, _, num = entries[entry].partition('|')
            _show_log(loglines, int(num), highlights, preview_height,
                      preview_width)
