#!/usr/bin/env python3

"""Swatbot review functions."""

import logging
import re
import shutil
from typing import Optional

import requests
from simple_term_menu import TerminalMenu  # type: ignore

from . import swatbuild
from . import utils
from .webrequests import Session

logger = logging.getLogger(__name__)


RESET = "\x1b[0m"
RED = "\x1b[1;31m"
GREEN = "\x1b[1;32m"
YELLOW = "\x1b[1;33m"
BLUE = "\x1b[1;34m"
PURPLE = "\x1b[1;35m"
CYAN = "\x1b[1;36m"
WHITE = "\x1b[1;37m"


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
                     highlight_lines: dict[int, tuple[str, str]]):
    if linenum == colorized_line:
        if linenum in highlight_lines:
            linecolor = highlight_lines[linenum][1]
        else:
            linecolor = CYAN
        text = f"{linecolor}{text}{RESET}"
    elif linenum in highlight_lines:
        pat = highlight_lines[linenum][0]
        color = highlight_lines[linenum][1]
        text = re.sub(pat, f"{color}{pat}{RESET}", text)
    return text


def _format_log_preview_line(linenum: int, text: str, colorized_line: int,
                             highlight_lines: dict[int, tuple[str, str]]):
    preview_text = text.replace('\t', '    ')
    formatted_text = _format_log_line(linenum, preview_text, colorized_line,
                                      highlight_lines)
    return f"{linenum: 6d} {formatted_text}"


def _get_preview_window(linenum: int, lines: list[str], preview_height: int
                        ) -> tuple[int, int]:
    start = max(0, linenum - int(preview_height / 4))
    end = start + preview_height
    if end >= len(lines):
        end = len(lines)
        start = max(0, end - preview_height)

    return (start, end)


def _format_log_preview(linenum: int, lines: list[str],
                        highlight_lines: dict[int, tuple[str, str]],
                        preview_height: int) -> str:
    start, end = _get_preview_window(linenum, lines, preview_height)
    lines = [_format_log_preview_line(i, t, linenum, highlight_lines)
             for i, t in enumerate(lines[start: end], start=start + 1)]
    return "\n".join(lines)


def _get_log_highlights(loglines: list[str]) -> dict[int, tuple[str, str]]:
    pats = [(re.compile(r"(.*\s|^)(?P<keyword>\S*error):", flags=re.I),
             RED),
            (re.compile(r"(.*\s|^)(?P<keyword>\S*warning):", flags=re.I),
             YELLOW),
            ]

    highlight_lines = {}
    for linenum, line in enumerate(loglines, start=1):
        for (pat, color) in pats:
            match = pat.match(line)
            if match:
                highlight_lines[linenum] = (match.group("keyword"), color)

    return highlight_lines


def _show_log(loglines: list[str], selected_line: Optional[int],
              highlight_lines: dict[int, tuple[str, str]],
              preview_height: Optional[int]):
    colorlines = [_format_log_line(i, t, selected_line, highlight_lines)
                  for i, t in enumerate(loglines, start=1)]

    startline: Optional[int]
    if selected_line and preview_height:
        startline, _ = _get_preview_window(selected_line, loglines,
                                           preview_height)
        startline += 1  # Use line number, not line index
    else:
        startline = selected_line
    utils.show_in_less("\n".join(colorlines), startline)


def _load_log(failure: swatbuild.Failure, logname: str
              ) -> Optional[str]:
    logurl = failure.get_log_raw_url(logname)
    if not logurl:
        logging.error("Failed to find log")
        return None

    try:
        logdata = Session().get(logurl)
    except requests.exceptions.ConnectionError:
        logger.warning("Failed to download stdio log")
        return None

    return logdata


def show_log_menu(failure: swatbuild.Failure, logname: str) -> bool:
    """Analyze a failure log file."""
    logdata = _load_log(failure, logname)
    if not logdata:
        return False

    utils.clear()
    loglines = logdata.splitlines()
    highlight_lines = _get_log_highlights(loglines)

    entries = ["View entire log file|",
               "View entire log file in default editor|",
               *[f"On line {line: 6d}: {highlight_lines[line][0]}|{line}"
                 for line in sorted(highlight_lines)]
               ]

    preview_size = 0.6
    termheight = shutil.get_terminal_size((80, 20)).lines
    preview_height = int(preview_size * termheight)

    def preview(line):
        return _format_log_preview(int(line), loglines, highlight_lines,
                                   preview_height)

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
            _show_log(loglines, None, highlight_lines, None)
        elif entry == 1:
            utils.launch_in_system_defaultshow_in_less(logdata)
        else:
            _, _, num = entries[entry].partition('|')
            _show_log(loglines, int(num), highlight_lines, preview_height)
