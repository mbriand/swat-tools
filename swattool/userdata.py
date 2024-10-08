#!/usr/bin/env python3

"""Interaction with the swatbot Django server."""

import collections
import logging
import pathlib
import shutil
from typing import Any, Optional

import yaml

from . import utils
from . import swatbot

logger = logging.getLogger(__name__)

USERINFOFILE = utils.DATADIR / "userinfos.yaml"


class Triage:
    """A failure new triage entry."""

    def __init__(self, values: Optional[dict] = None):
        self.failures: list[int] = []
        self.status = swatbot.TriageStatus.PENDING
        self.comment = ""
        self.extra: dict[str, Any] = {}

        if values:
            try:
                failures = values['failures']
                status = swatbot.TriageStatus.from_str(values['status'])
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
        """Export data as a dictionary."""
        return {'failures': self.failures,
                'status': self.status.name,
                'comment': self.comment,
                **self.extra
                }

    def __str__(self):
        return f"{self.status.name.title()}: {self.comment}"


class UserInfo:
    """A failure user data."""

    def __init__(self, values: Optional[dict] = None):
        if values:
            self.notes = values.get('notes', [])
            self.triages = [Triage(t) for t in values.get('triages', [])]
        else:
            self.notes = []
            self.triages = []

    def get_notes(self) -> str:
        """Get formatted user notes."""
        return "\n\n".join(self.notes)

    def set_notes(self, notes: Optional[str]):
        """Set user notes."""
        if not notes:
            self.notes = []
        else:
            self.notes = [n.strip() for n in notes.split("\n\n")]

    def as_dict(self) -> dict:
        """Export data as a dictionary."""
        data = {}
        if self.notes:
            data['notes'] = self.notes
        if self.triages:
            data['triages'] = [triage.as_dict() for triage in self.triages]

        return data

    def get_failure_triage(self, failureid: int) -> Optional[Triage]:
        """Get the Triage corresponding to a given failure id."""
        for triage in self.triages:
            if failureid in triage.failures:
                return triage

        return None


class UserInfos(collections.abc.MutableMapping):
    """A collection of failure user data."""

    def __init__(self):
        self.infos = {}
        self.load()

    def load(self):
        """Load user infos stored during previous review session."""
        logger.info("Loading saved data...")
        if USERINFOFILE.exists():
            with USERINFOFILE.open('r') as file:
                pretty_userinfos = yaml.load(file, Loader=yaml.Loader)
                self.infos = pretty_userinfos
                self.infos = {bid: UserInfo(info)
                              for bid, info in pretty_userinfos.items()}

    def save(self, suffix="") -> pathlib.Path:
        """Store user infos for later runs."""
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
        i = 0
        while filename.with_stem(f'{filename.stem}-backup-{i}').exists():
            i += 1
        shutil.copy(filename,
                    filename.with_stem(f'{filename.stem}-backup-{i}'))

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
