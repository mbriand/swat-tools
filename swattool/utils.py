#!/usr/bin/env python3

"""Various helpers with no better place to."""

import pathlib
import subprocess

BINDIR = pathlib.Path(__file__).parent.parent.resolve()
DATADIR = BINDIR / "data"
CACHEDIR = DATADIR / "cache"

MAILNAME = subprocess.run(["git", "config", "--global", "user.name"],
                          capture_output=True).stdout.decode().strip()
