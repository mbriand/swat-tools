#!/usr/bin/env python3

"""Various helpers with no better place to."""

import pathlib
import subprocess
from typing import Optional

import xdg

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
