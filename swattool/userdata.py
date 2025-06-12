#!/usr/bin/env python3

"""Interaction with the swatbot Django server.

This module provides classes for storing and managing user-specific data
related to build failures, such as notes and triage information.
"""

import collections
import logging
import pathlib
import shutil
import textwrap
from typing import Any, Optional

import yaml

from . import utils
from . import swatbotrest
from .bugzilla import Bugzilla

logger = logging.getLogger(__name__)

USERINFOFILE = utils.DATADIR / "userinfos.yaml"


class Triage:
    """A failure new triage entry.

    Represents a triage decision for one or more failures.
    """

    def __init__(self, values: Optional[dict] = None):
        self.failures: list[int] = []
        self.status = swatbotrest.TriageStatus.PENDING
        self.comment = ""
        self.extra: dict[str, Any] = {}

        if values:
            try:
                failures = values['failures']
                status = swatbotrest.TriageStatus.from_str(values['status'])
                comment = values['comment']
                extra = {k: v for k, v in values.items()
                         if k not in self.__dict__}
            except KeyError:
                pass
            else:
                self.failures = failures
                self.status = status
                self.comment = comment
                self.extra = extra

    def as_dict(self) -> dict:
        """Export data as a dictionary.

        Returns:
            Dictionary representation of the triage entry
        """
        return {'failures': self.failures,
                'status': self.status.name,
                'comment': self.comment,
                **self.extra
                }

    def __str__(self):
        return f"{str(self.status)}: {self.comment}"

    def format_description(self) -> str:
        """Get info on one given Triage in a pretty way.

        Returns:
            Formatted description of the triage status
        """
        statusfrags = []

        statusname = self.status.name.title()
        statusfrags.append(f"{statusname}: {self.comment}")

        if self.status == swatbotrest.TriageStatus.BUG:
            bugid = int(self.comment)
            bugtitle = Bugzilla.get_bug_title(bugid)
            if bugtitle:
                statusfrags.append(f", {bugtitle}")
            else:
                nf = utils.Color.colorize("NOT FOUND", utils.Color.RED)
                statusfrags.append(f", {nf}")

        bzcomment = self.extra.get('bugzilla-comment')
        if bzcomment:
            statusfrags.append("\n")
            bcomlines = bzcomment.split('\n')
            bcom = [textwrap.fill(line) for line in bcomlines]
            statusfrags.append("\n".join(bcom))

        return "".join(statusfrags)


class UserInfo:
    """A failure user data.

    Stores user-specific data about a build, including notes and triage decisions.
    """

    def __init__(self, values: Optional[dict] = None):
        if values:
            self.notes = values.get('notes', [])
            self.triages = [Triage(t) for t in values.get('triages', [])]
        else:
            self.notes = []
            self.triages = []

    def get_notes(self) -> str:
        """Get formatted user notes.

        Returns:
            All notes joined with double newlines
        """
        return "\n\n".join(self.notes)

    def get_wrapped_notes(self, width: int, indent: str):
        """Get formatted and wrapped user notes.

        Args:
            width: Maximum width for wrapped text
            indent: String to use for indentation

        Returns:
            Formatted and wrapped notes
        """
        wrapped_lns = ["\n".join([textwrap.indent(li, indent)
                                  for line in note.split("\n")
                                  for li in textwrap.wrap(line, width)
                                  ])
                       for note in self.notes]
        return "\n\n".join(wrapped_lns)

    def set_notes(self, notes: Optional[str]):
        """Set user notes.

        Args:
            notes: String containing notes, with paragraphs separated by double newlines
        """
        if not notes:
            self.notes = []
        else:
            self.notes = [n.strip() for n in notes.split("\n\n")]

    def as_dict(self) -> dict:
        """Export data as a dictionary.

        Returns:
            Dictionary representation of user info, including notes and triages
        """
        data = {}
        if self.notes:
            data['notes'] = self.notes
        if self.triages:
            data['triages'] = [triage.as_dict() for triage in self.triages]

        return data

    def get_failure_triage(self, failureid: int) -> Optional[Triage]:
        """Get the Triage corresponding to a given failure id.

        Args:
            failureid: ID of the failure to find triage for

        Returns:
            Triage object for the failure or None if not found
        """
        for triage in self.triages:
            if failureid in triage.failures:
                return triage

        return None

    def __repr__(self):
        return repr(self.as_dict())


class UserInfos(collections.abc.MutableMapping):
    """A collection of failure user data.

    Maps build IDs to UserInfo objects and provides persistence.
    """

    def __init__(self):
        self.infos = {}
        self.load()

    def load(self):
        """Load user infos stored during previous review session.

        Reads user information from the YAML file if it exists.
        """
        logger.info("Loading saved data...")
        if USERINFOFILE.exists():
            with USERINFOFILE.open('r') as file:
                pretty_userinfos = yaml.load(file, Loader=yaml.SafeLoader)
                if not pretty_userinfos:
                    pretty_userinfos = {}
                self.infos = {bid: UserInfo(info)
                              for bid, info in pretty_userinfos.items()}

    def save(self, suffix="") -> pathlib.Path:
        """Store user infos for later runs.

        Saves user information to a YAML file and creates a backup.

        Args:
            suffix: Optional suffix to append to the filename

        Returns:
            Path to the saved file
        """
        # Cleaning old reviews
        for info in self.infos.values():
            info.triages = [t for t in info.triages if t.failures]

        pretty_userinfos = {bid: info.as_dict()
                            for bid, info in self.infos.items()
                            if info.as_dict()}

        filename = USERINFOFILE.with_stem(f'{USERINFOFILE.stem}{suffix}')
        with filename.open('w') as file:
            yaml.dump(pretty_userinfos, file)

        # Create backup files. We might remove this once the code becomes more
        # stable
        backupfile = filename.parent / 'backups' / filename.name
        i = 0
        while backupfile.with_stem(f'{filename.stem}-backup-{i}').exists():
            i += 1
        shutil.copy(filename,
                    backupfile.with_stem(f'{filename.stem}-backup-{i}'))

        return filename

    def __getitem__(self, buildid: int) -> UserInfo:
        return self.infos.setdefault(buildid, UserInfo())

    def __setitem__(self, buildid: int, value: UserInfo):
        self.infos[buildid] = value

    def __delitem__(self, buildid: int):
        del self.infos[buildid]

    def __len__(self):
        return len(self.infos)

    def __iter__(self):
        return iter(self.infos)

    def __repr__(self):
        return ", ".join(repr(info) for info in self.infos.items())
