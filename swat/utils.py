#!/usr/bin/env python3

"""Various helpers with no better place to."""

import os
import pathlib
import shlex
import subprocess
import tempfile
from typing import Optional

BINDIR = pathlib.Path(__file__).parent.parent.resolve()
DATADIR = BINDIR / "data"
CACHEDIR = DATADIR / "cache"

MAILNAME = subprocess.run(["git", "config", "--global", "user.name"],
                          capture_output=True).stdout.decode().strip()


def edit_text(text: Optional[str]) -> str:
    """Edit a text string in user preferred editor."""
    editor = os.environ.get("EDITOR", "vim")

    with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
        if text:
            f.write(text)
        f.close()
        subprocess.run(shlex.split(f"{editor} {f.name}"))
        with open(f.name, mode='r') as fr:
            newtext = fr.read()
        os.unlink(f.name)

    return newtext
