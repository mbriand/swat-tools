#!/usr/bin/env python3

"""Various helpers with no better place to.

This module contains utility functions used throughout the swattool
application.
"""

import atexit
import concurrent.futures
import contextlib
import logging
import os
import readline
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Iterable, Optional

from simple_term_menu import TerminalMenu  # type: ignore
import click
import tabulate
from tqdm.contrib.logging import tqdm_logging_redirect
import xdg  # type: ignore

CONFIG_FILE = xdg.xdg_config_home() / "swattool.toml"
DATADIR = xdg.xdg_cache_home() / "swattool"
CACHEDIR = DATADIR / "cache"

logger = logging.getLogger(__name__)


def _get_git_username() -> Optional[str]:
    """Get the global Git username.

    Returns:
        The global Git username or None if not found.
    """
    try:
        process = subprocess.run(["git", "config", "--global", "user.name"],
                                 capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    return process.stdout.decode().strip()


MAILNAME = _get_git_username()


def load_config() -> dict:
    """Load configuration from the user's config file.

    Loads the TOML configuration file from ~/.config/swattool.toml if it
    exists, otherwise returns an empty dictionary.

    Returns:
        Dictionary containing the configuration data, or empty dict if no
        config file found
    """
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


class Color:
    """Text color handling.

    Provides ANSI color codes and methods to colorize terminal output.
    """

    # pylint: disable=too-few-public-methods

    RESET = "\x1b[0m"
    RED = "\x1b[1;31m"
    GREEN = "\x1b[1;32m"
    YELLOW = "\x1b[1;33m"
    BLUE = "\x1b[1;34m"
    PURPLE = "\x1b[1;35m"
    CYAN = "\x1b[1;36m"
    WHITE = "\x1b[1;37m"
    NONE = ""

    @classmethod
    def colorize(cls, text: str, color: Optional[str]) -> str:
        """Colorize a string.

        Args:
            text: The text to colorize
            color: ANSI color code to use

        Returns:
            Colorized text with reset code appended
        """
        if not color:
            return text
        return f"{color}{text}{cls.RESET}"


class SwattoolException(Exception):
    """A generic swattool error.

    Base exception class for all swattool exceptions.
    """


class LoginRequiredException(SwattoolException):
    """An exception for operations requiring login.

    Raised when an operation requires login but the user is not logged in.
    """

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
    """Create logging handlers and setup logging configuration.

    Args:
        verbose: Verbosity level for logging
    """
    loglevel = logging.DEBUG if verbose >= 1 else logging.INFO

    defhandler = logging.StreamHandler()
    if sys.stdout.isatty():
        defhandler.setFormatter(_PrettyLogFormatter())
    else:
        defhandler.setFormatter(_SimpleLogFormatter())
    handlers: list[logging.StreamHandler] = [defhandler]

    logging.basicConfig(level=loglevel, handlers=handlers)

    if verbose < 2:
        logging.getLogger("charset_normalizer").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def _save_readline(history_file):
    history_file.parent.mkdir(parents=True, exist_ok=True)
    readline.write_history_file(history_file)


def setup_readline():
    """Initialize readline history manager.

    Loads history from file and sets up saving on exit.
    """
    history_file = DATADIR / "history"
    with contextlib.suppress(FileNotFoundError):
        readline.read_history_file(history_file)

    atexit.register(_save_readline, history_file)


def clear():
    """Clear the screen.

    Does not clear screen in debug mode to preserve log messages.
    """
    if logging.getLogger().level <= logging.DEBUG:
        # Debug logging: never clear screen, to preserve traces
        return

    click.clear()


def tabulated_menu(entries: Iterable[Iterable[Any]], **kwargs) -> TerminalMenu:
    """Generate a TerminalMenu with tabulated lines.

    Args:
        entries: List of menu entries
        **kwargs: Additional arguments for TerminalMenu

    Returns:
        TerminalMenu: A terminal menu with formatted tabular entries
    """
    tabulated_entries = tabulate.tabulate(entries, tablefmt="plain")
    return TerminalMenu(tabulated_entries.splitlines(),
                        raise_error_on_interrupt=True, **kwargs)


def show_in_less(text: str, startline: Optional[int] = 0):
    """Show a text buffer in less program.

    Args:
        text: Text content to display
        startline: Line number to start displaying from (0 for start)
    """
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


def launch_in_system_default(text: str):
    """Show a text buffer in system default program.

    Args:
        text: Text content to display
    """
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as file:
        file.write(text)
        file.close()
        click.launch(file.name)


class ExecutorWithProgress:
    """Generate a thread pool executor with progress bar.

    Manages concurrent execution of jobs with a visual progress indicator.
    """

    def __init__(self, threads: Optional[int] = None):
        if threads is None:
            cpus = os.cpu_count()
            threads = min(16, cpus) if cpus else 16
        self.executor = concurrent.futures.ThreadPoolExecutor(threads)
        self.jobs: list[tuple[str, concurrent.futures.Future]] = []

    def submit(self, name, *args, **kwargs):
        """Submit a new job to the executor.

        Args:
            name: Display name for the job
            *args: Positional arguments for the job function
            **kwargs: Keyword arguments for the job function
        """
        self.jobs.append((name, self.executor.submit(*args, **kwargs)))

    def run(self):
        """Run all jobs in the executor.

        Displays a progress bar showing job completion status.
        """
        bar_format = "{l_bar}{bar}| [{elapsed}<{remaining}{postfix}]"
        with tqdm_logging_redirect(total=len(self.jobs),
                                   desc="Loading failures",
                                   bar_format=bar_format
                                   ) as jobsprogress:
            try:
                alljobs = [job[1] for job in self.jobs]
                for fut in concurrent.futures.as_completed(alljobs):
                    err = fut.exception()
                    if (err and isinstance(err, SwattoolException)):
                        logging.warning(str(err))
                    elif err:
                        raise err
                    stepname = next((jobname for jobname, job in self.jobs
                                    if job.running()), "")
                    jobsprogress.update()
                    jobsprogress.set_postfix_str(str(stepname))
            except KeyboardInterrupt:
                self.executor.shutdown(cancel_futures=True)
            except Exception:
                self.executor.shutdown(cancel_futures=True)
                raise
