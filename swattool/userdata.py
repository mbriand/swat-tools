#!/usr/bin/env python3

"""Interraction with the swatbot Django server."""

import collections
import logging
import pathlib
import shutil
from typing import Optional

import yaml

from . import utils

logger = logging.getLogger(__name__)

USERINFOFILE = utils.DATADIR / "userinfos.yaml"


class UserInfo:
    """A failure user data."""

    def __init__(self):
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
                # self.infos = {bid: {swatbuild.Field(k): v
                #                     for k, v in info.items()}
                #               for bid, info in pretty_userinfos.items()}

    def save(self, suffix="") -> pathlib.Path:
        """Store user infos for later runs."""
        # Cleaning old reviews
        for info in self.infos.values():
            info.triages = [t for t in info.triages if t['failures']]

        # TODO ?
        # pretty_userinfos = {bid: {str(k): v for k, v in info.items()}
        #                     for bid, info in self.infos.items()
        #                     if info.triages or info.notes}
        pretty_userinfos = {bid: info
                            for bid, info in self.infos.items()
                            if info.triages or info.notes}

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
