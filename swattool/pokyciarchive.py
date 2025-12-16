#!/usr/bin/env python3

"""Helper to retrieve the list of commits from a given build.

This module provides functionality to manage and query a local git repository
of the Poky CI archive, which contains build information for Yocto Project.
"""

import logging
import signal
import subprocess
import time
from typing import Any, Optional

import pygit2  # type: ignore

from . import utils

logger = logging.getLogger(__name__)

GITDIR = utils.DATADIR / 'gits' / 'poky-ci-archive'

remotes = {
    "poky-ci-archive": 'https://git.yoctoproject.org/poky-ci-archive',
    "poky": 'https://git.yoctoproject.org/poky',
    "bitbake": 'https://git.openembedded.org/bitbake',
    "meta-yocto": 'https://git.yoctoproject.org/meta-yocto',
    "oecore": 'https://git.openembedded.org/openembedded-core',
}


def update(min_age: Optional[int] = None) -> None:
    """Update the CI archive git repository.

    Updates the local clone of the Poky CI archive repository or creates
    it if it doesn't exist. Can skip update if recently fetched.

    Args:
        min_age: Minimum age in seconds since last fetch to perform update
    """
    if GITDIR.exists():
        repo = pygit2.Repository(GITDIR)

        if min_age:
            fetch_head = GITDIR / "FETCH_HEAD"
            ctime = fetch_head.stat().st_ctime
            if time.time() - ctime < min_age:
                return
    else:
        GITDIR.parent.mkdir(parents=True, exist_ok=True)
        repo = pygit2.clone_repository(remotes["poky-ci-archive"], GITDIR,
                                       bare=True)

    for name, url in remotes.items():
        if name in repo.remotes.names():
            repo.remotes.set_url(name, url)
        else:
            repo.remotes.create(name, url)

    for remote in repo.remotes:
        try:
            remote.fetch(remote.fetch_refspecs + ['--tags'])
        except pygit2.GitError as e:
            logger.warning("Failed to update %s: %s", remote.name, str(e))


def get_build_commits(buildname: str, git_name: str,
                      basebranch: str = "master", limit: int = 100
                      ) -> Optional[dict[str, Any]]:
    """Get the list of commits ahead of master for a given build.

    Analyzes the git history to find commits specific to a build by comparing
    with the base branch.

    Args:
        buildname: Name of the build/tag to query
        git_name: Name of the git remote to use for branch lookup
        basebranch: Base branch to compare against
        limit: Maximum number of commits to return

    Returns:
        Dictionary with commit information or None if build not found
    """
    repo = pygit2.Repository(GITDIR)
    tagname = f'refs/tags/{buildname}'
    branchname = f'refs/remotes/{git_name}/{basebranch}'

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


def show_log(tip: str, base: Optional[str] = None,
             options: Optional[list[str]] = None) -> bool:
    """Show git log between two commits.

    Opens git log viewer (less) to show commit history.

    Args:
        tip: Commit hash or reference for the newest commit
        base: Commit hash or reference for the oldest commit
        options: Additional git log options

    Returns:
        True if successful, False otherwise
    """
    if options is None:
        options = []
    gitcmd = ["git", "-C", GITDIR, "-c", "core.pager=less",
              "log", *options,
              f"{base}..{tip}" if base else tip]
    try:
        subprocess.run(gitcmd, check=True)
    except subprocess.CalledProcessError as e:
        # Ignore sigpipe errors, as maybe the user will not read the whole log
        if e.returncode != -signal.SIGPIPE:
            logger.error("Failed to show git log")
            return False

    return True
