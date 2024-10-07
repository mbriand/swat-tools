#!/usr/bin/env python3

"""Various helpers with no better place to."""

import logging
import sys
import pathlib
import subprocess
from typing import Optional

import xdg  # type: ignore

BINDIR = pathlib.Path(__file__).parent.parent.resolve()
DATADIR = xdg.xdg_cache_home() / "swattool"
CACHEDIR = DATADIR / "cache"


def _get_git_username() -> Optional[str]:
    try:
        process = subprocess.run(["git", "config", "--global", "user.name"],
                                 capture_output=True, check=True)
    except Exception:
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
    # logformat = "{color}%(asctime)s [%(levelname)s] %(name)s:{reset} " \
    #     "%(message)s"
    logformat = "{color}%(message)s{reset}"

    def _format(self, record, color):
        if color:
            reset = self.reset
        else:
            color = reset = ""

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
