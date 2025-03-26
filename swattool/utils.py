#!/usr/bin/env python3

"""Various helpers with no better place to."""

import concurrent.futures
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


class Color:
    """Text color handling."""

    # pylint: disable=too-few-public-methods

    RESET = "\x1b[0m"
    RED = "\x1b[1;31m"
    GREEN = "\x1b[1;32m"
    YELLOW = "\x1b[1;33m"
    BLUE = "\x1b[1;34m"
    PURPLE = "\x1b[1;35m"
    CYAN = "\x1b[1;36m"
    WHITE = "\x1b[1;37m"

    @classmethod
    def colorize(cls, text: str, color: str) -> str:
        """Colorize a string."""
        return f"{color}{text}{cls.RESET}"


class SwattoolException(Exception):
    """A generic swattool error."""


class LoginRequiredException(SwattoolException):
    """An exception for operations requiring login."""

    def __init__(self, message, service):
        super().__init__(message)

        self.service = service


class _LogFormatter(logging.Formatter):
    colors = {
        logging.DEBUG: Color.WHITE,
        logging.WARNING: Color.YELLOW,
        logging.ERROR: Color.RED,
        logging.CRITICAL: Color.RED,
    }
    detail_logformat = "{color}[%(levelname)s] %(name)s: %(message)s{reset}"
    logformat = "{color}%(message)s{reset}"

    def _format(self, record, color):
        if color:
            reset = Color.RESET
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
    less_cmd = ["less", "-N", "-i"]
    if startline:
        less_cmd.append(f"+G{startline}")

    with tempfile.NamedTemporaryFile(mode='w', delete=False) as file:
        file.write(text)
        file.close()
        try:
            subprocess.run([*less_cmd, file.name], check=True)
        except subprocess.CalledProcessError:
            logger.error("Failed to start less")
        os.unlink(file.name)


def launch_in_system_defaultshow_in_less(text: str):
    """Show a text buffer in system default program."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as file:
        file.write(text)
        file.close()
        click.launch(file.name)


class ExecutorWithProgress:
    """Generate a thread pool executor with progress bar."""

    def __init__(self, threads: Optional[int] = None):
        if threads is None:
            cpus = os.cpu_count()
            threads = min(16, cpus) if cpus else 16
        self.executor = concurrent.futures.ThreadPoolExecutor(threads)
        self.jobs: list[tuple[str, concurrent.futures.Future]] = []

    def submit(self, name, *args, **kwargs):
        """Submit a new job to the executor."""
        self.jobs.append((name, self.executor.submit(*args, **kwargs)))

    def run(self):
        """Run all jobs in the executor."""
        with click.progressbar(length=len(self.jobs), label="Loading failures",
                               item_show_func=lambda a: a
                               ) as jobsprogress:
            try:
                alljobs = [job[1] for job in self.jobs]
                for _ in concurrent.futures.as_completed(alljobs):
                    stepname = next((jobname for jobname, job in self.jobs
                                    if job.running()), "")
                    jobsprogress.update(1, str(stepname))
            except KeyboardInterrupt:
                self.executor.shutdown(cancel_futures=True)
            except Exception:
                self.executor.shutdown(cancel_futures=True)
                raise
