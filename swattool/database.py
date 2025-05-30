#!/usr/bin/env python3

"""Interaction with the swatbot Django server.

This module provides functionality for retrieving build failures data from the
swatbot server.
"""

import logging
import sqlite3
from typing import Any, Collection

from . import logfingerprint
from . import swatbotrest
from . import swatbuild
from . import swatlogs
from . import userdata
from . import utils

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self._db = sqlite3.connect(utils.DATADIR / "swattool.db")
        cur = self._db.cursor()
        cur.execute("CREATE TABLE build(id PRIMARY KEY, status, test, worker, "
                    "completed, collection_id, ab_url, owner, branch, "
                    "parent_id)")
        cur.execute("CREATE TABLE collection(id PRIMARY KEY, "
                    "owner, branch, build_id)")
        cur.execute("CREATE TABLE failures(id PRIMARY KEY, build_id)")

