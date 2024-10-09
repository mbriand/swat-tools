#!/usr/bin/env python3

"""Various helpers with no better place to."""

import logging
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Optional

from simple_term_menu import TerminalMenu  # type: ignore

import click
import tabulate
import xdg  # type: ignore

BINDIR = pathlib.Path(__file__).parent.parent.resolve()
DATADIR = xdg.xdg_cache_home() / "swattool"
CACHEDIR = DATADIR / "cache"

logger = logging.getLogger(__name__)


def _get_git_username() -> Optional[str]:
    try:
        process = subprocess.run(["git", "config", "--global", "user.name"],
                                 capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    return process.stdout.decode().strip()


MAILNAME = _get_git_username()


class SwattoolException(Exception):
    """A generic swattool error."""


class _LogFormatter(logging.Formatter):
    colors = {
        logging.DEBUG: "\x1b[1;38m",
        logging.WARNING: "\x1b[1;33m",
        logging.ERROR: "\x1b[1;31m",
        logging.CRITICAL: "\x1b[1;31m",
    }
    reset = "\x1b[0m"
    detail_logformat = "{color}[%(levelname)s] %(name)s: %(message)s{reset}"
    logformat = "{color}%(message)s{reset}"

    def _format(self, record, color):
        if color:
            reset = self.reset
        else:
            color = reset = ""

        if record.levelno == logging.DEBUG:
            log_fmt = self.detail_logformat.format(color=color, reset=reset)
        else:
            log_fmt = self.logformat.format(color=color, reset=reset)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


class _SimpleLogFormatter(_LogFormatter):
    def format(self, record):
        return self._format(record, None)


class _PrettyLogFormatter(_LogFormatter):
    def format(self, record):
        return self._format(record, self.colors.get(record.levelno))


def setup_logging(verbose: int):
    """Create logging handlers ans setup logging configuration."""
    if verbose >= 1:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO

    defhandler = logging.StreamHandler()
    if sys.stdout.isatty():
        defhandler.setFormatter(_PrettyLogFormatter())
    else:
        defhandler.setFormatter(_SimpleLogFormatter())
    handlers: list[logging.StreamHandler] = [defhandler]

    logging.basicConfig(level=loglevel, handlers=handlers)


def clear():
    """Clear the screen."""
    if logging.getLogger().level <= logging.DEBUG:
        # Debug logging: never clear screen, to preserve traces
        return

    click.clear()


def tabulated_menu(entries: Iterable[Iterable[Any]], **kwargs) -> TerminalMenu:
    """Generate a TerminalMenu with tabulated lines."""
    tabulated_entries = tabulate.tabulate(entries, tablefmt="plain")
    return TerminalMenu(tabulated_entries.splitlines(),
                        raise_error_on_interrupt=True, **kwargs)


def show_in_less(text: str, startline: Optional[int] = 0):
    """Show a text buffer in less program."""
    less_cmd = ["less", "-N"]
    if startline:
        less_cmd.append(f"+G{startline}")

    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write(text)
        f.close()
        try:
            subprocess.run([*less_cmd, f.name], check=True)
        except subprocess.CalledProcessError:
            logger.error("Failed to start less")
        os.unlink(f.name)


def launch_in_system_defaultshow_in_less(text: str):
    """Show a text buffer in system default program."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        f.write(text)
        f.close()
        click.launch(f.name)
