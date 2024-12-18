#!/usr/bin/env python3

"""Helper to retrieve the list of commits from a given build."""

import logging
import subprocess
from typing import Any, Optional

import pygit2  # type: ignore

from . import utils

logger = logging.getLogger(__name__)

GITDIR = utils.DATADIR / 'gits' / 'poky-ci-archive'
POKYGIT_GITURL = 'https://git.yoctoproject.org/poky'
ARCHIVE_GITURL = 'https://git.yoctoproject.org/poky-ci-archive'


def update() -> None:
    """Update the CI archive git."""
    if GITDIR.exists():
        repo = pygit2.Repository(GITDIR)
    else:
        GITDIR.parent.mkdir(parents=True, exist_ok=True)
        repo = pygit2.clone_repository(POKYGIT_GITURL, GITDIR, bare=True)
        repo.remotes.create("archive", ARCHIVE_GITURL)

    for remote in repo.remotes:
        remote.fetch(remote.fetch_refspecs + ['refs/tags/*:refs/tags/*'])


def get_build_commits(buildname: str, basebranch: str = "master",
                      limit: int = 100
                      ) -> Optional[dict[str, Any]]:
    """Get the list of commits ahead of master for a given build."""
    repo = pygit2.Repository(GITDIR)
    tagname = f'refs/tags/{buildname}'
    branchname = f'refs/remotes/origin/{basebranch}'

    if tagname not in repo.references or branchname not in repo.references:
        return None

    tag = repo.revparse(tagname).from_object.id
    branch = repo.revparse(branchname).from_object.id
    mergebase = repo.merge_base(tag, branch)

    commits: list[pygit2.Object] = []
    for commit in repo.walk(tag):
        if len(commits) > limit or commit.id == mergebase:
            break
        commits.append(commit)

    return {'base_commit': branch,
            'tip_commit': tag,
            'commits': commits,
            }


def show_log(tip: str, base: Optional[str] = None) -> bool:
    """Show git log."""
    gitcmd = ["git", "-C", GITDIR, "log", f"{base}..{tip}" if base else tip]
    try:
        subprocess.run(gitcmd, check=True)
    except subprocess.CalledProcessError:
        logger.error("Failed to show git log")
        return False

    return True
