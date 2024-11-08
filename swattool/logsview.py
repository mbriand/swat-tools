#!/usr/bin/env python3

"""Swatbot review functions."""

import logging
import re
import shutil
from typing import Optional

from simple_term_menu import TerminalMenu  # type: ignore

from . import swatbuild
from . import utils

logger = logging.getLogger(__name__)


class _Highlight:
    # pylint: disable=too-few-public-methods
    def __init__(self, keyword: str, color: str, in_menu: bool):
        self.keyword = keyword
        self.color = color
        self.in_menu = in_menu


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

        hl = _Highlight(match.group("keyword"), self.color, self.in_menu)
        return (True, hl)


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


def _format_log_preview_line(linenum: int, text: str, colorized_line: int,
                             highlight_lines: dict[int, _Highlight],
                             preview_width: int):
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


def _get_log_highlights(loglines: list[str], failure: swatbuild.Failure
                        ) -> dict[int, _Highlight]:
    status = failure.build.status
    test = failure.build.test
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
        #  - Do nothing on "libgpg-error:"
        #  - Match on "error:", show in menu if build status is error.
        #  - Match on "warning:", show in menu if build status is warning.
        _Filter(re.compile(r".*libgpg-error:"), True, None, False),
        _Filter(re.compile(r"(.*\s|^)(?P<keyword>\S*error):", flags=re.I),
                True, utils.Color.RED, status == swatbuild.Status.ERROR),
        _Filter(re.compile(r"(.*\s|^)(?P<keyword>\S*warning):",
                           flags=re.I),
                True, utils.Color.YELLOW, status == swatbuild.Status.WARNING),
    ]

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


def _get_cached_log_highlights(failure: swatbuild.Failure, logname: str,
                               loglines: list[str]
                               ) -> dict[int, _Highlight]:
    highlights = _cached_log_highlights.get((failure, logname), None)
    if highlights:
        return highlights

    highlights = _get_log_highlights(loglines, failure)
    _cached_log_highlights[(failure, logname)] = highlights
    return highlights


def get_log_highlights(failure: swatbuild.Failure, logname: str
                       ) -> list[str]:
    """Get log highlights for a given log file."""
    logdata = failure.get_log(logname)
    if not logdata:
        return []

    loglines = logdata.splitlines()

    highlights = _get_cached_log_highlights(failure, logname, loglines)

    return [loglines[line - 1] for line in highlights
            if highlights[line].in_menu]


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
    highlights = _get_cached_log_highlights(failure, logname, loglines)

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
