#!/usr/bin/env python3

"""Swatbot log functions."""

import logging
import shutil
from typing import Optional

from simple_term_menu import TerminalMenu  # type: ignore

from . import swatlogs
from . import swatbuild
from . import utils

logger = logging.getLogger(__name__)


class LogView:
    """Log viewer."""

    # pylint: disable=too-few-public-methods

    def __init__(self, failure: swatbuild.Failure, logname: str):
        self.failure = failure
        self.logname = logname
        self.log = swatlogs.Log(self.failure, self.logname)

        self.preview_size = 0.6
        self.preview_height = self.preview_width = 0

    def show_menu(self) -> bool:
        """Analyze a failure log file."""
        logdata = self.log.get_data()
        if not logdata:
            return False

        utils.clear()
        loglines = logdata.splitlines()
        highlights = self.log.get_highlights()

        entries = ["View entire log file|",
                   "View entire log file in default editor|",
                   *[f"On line {line: 6d}: {highlights[line].keyword}|{line}"
                     for line in sorted(highlights)
                     if highlights[line].in_menu]
                   ]

        def preview(line):
            return self._format_preview(int(line), loglines)

        title = f"{self.failure.build.format_short_description()}: " \
                f"{self.logname} of step {self.failure.stepnumber}"
        entry = 2
        while True:
            menu = TerminalMenu(entries, title=title, cursor_index=entry,
                                preview_command=preview,
                                preview_size=self.preview_size,
                                raise_error_on_interrupt=True)
            entry = menu.show()
            if entry is None:
                return True

            if entry == 0:
                self._show(loglines, None)
            elif entry == 1:
                utils.launch_in_system_defaultshow_in_less(logdata)
            else:
                _, _, num = entries[entry].partition('|')
                self._show(loglines, int(num))

    def _get_preview_window(self, linenum: int, lines: list[str],
                            ) -> tuple[int, int]:
        # All values below are in line index in the lines list, not line
        # numbers.
        lineidx = linenum - 1

        # Place the start on given line and rewind until we have desired height
        # before our line.
        start = lineidx
        before_len = 0
        target_before_len = int(self.preview_height / 3)
        while start > 0 and before_len < target_before_len:
            nextline = start - 1
            linecount = len(self._split_preview_line(lines[nextline]))
            if before_len + linecount > target_before_len:
                break

            start = nextline
            before_len += linecount

        # Place the end on given line and add lines until we reach full height.
        end = lineidx
        total_len = before_len
        total_len += len(self._split_preview_line(lines[end]))
        while end < len(lines) - 1 and total_len < self.preview_height:
            end += 1
            total_len += len(self._split_preview_line(lines[end]))

        # Special case on end of buffer: add some lines before.
        while start > 0 and total_len < self.preview_height:
            nextline = start - 1
            linecount = len(self._split_preview_line(lines[nextline]))
            if total_len + linecount > self.preview_height:
                break

            start = nextline
            total_len += linecount

        return (start, end)

    def _show(self, loglines: list[str], selected_line: Optional[int]):
        colorlines = [self._format_line(i, t, selected_line)
                      for i, t in enumerate(loglines, start=1)]

        startline: Optional[int]
        if selected_line and self.preview_height and self.preview_width:
            startline, _ = self._get_preview_window(selected_line, loglines)
            startline += 1  # Use line number, not line index
        else:
            startline = selected_line
        utils.show_in_less("\n".join(colorlines), startline)

    def _format_line(self, linenum: int, text: str,
                     colorized_line: Optional[int]):
        highlight_lines = self.log.get_highlights()
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

    def _split_preview_line(self, text: str):
        preview_text = text.expandtabs(4)
        width = self.preview_width - (1 + 6 + 1)  # space + line number + space
        return [preview_text[offset:offset + width]
                for offset in range(0, max(1, len(preview_text)), width)]

    @staticmethod
    def _escape_line(text):
        return repr(text)[1:-1]

    def _format_preview_line(self, linenum: int, text: str,
                             colorized_line: int):
        text = self._escape_line(text)
        for i, wrappedtext in enumerate(self._split_preview_line(text)):
            formatted_text = self._format_line(linenum, wrappedtext,
                                               colorized_line)
            if i == 0:
                yield f"{linenum: 6d} {formatted_text}"
            else:
                yield f"{' ' * 6} {formatted_text}"

    def _update_preview_size(self):
        termsize = shutil.get_terminal_size((80, 20))
        self.preview_height = int(self.preview_size * termsize.lines)
        self.preview_width = termsize.columns - 2  # Borders

    def _format_preview(self, linenum: int, lines: list[str]) -> str:
        self._update_preview_size()
        start, end = self._get_preview_window(linenum, lines)
        lines = [previewline
                 for i, t in enumerate(lines[start: end + 1], start=start + 1)
                 for previewline in self._format_preview_line(i, t, linenum)
                 ]
        return "\n".join(lines[:self.preview_height])


def show_logs_menu(build: swatbuild.Build):
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

        log = LogView(*logs[newentry])
        log.show_menu()
