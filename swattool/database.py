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
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cur.fetchall()]

        if 'build' not in tables:
            cur.execute("CREATE TABLE build(id PRIMARY KEY, status, test, "
                        "worker, completed, collection_id, ab_url, owner, "
                        "branch, parent_id)")
        if 'collection' not in tables:
            cur.execute("CREATE TABLE collection(id PRIMARY KEY, "
                        "owner, branch, build_id)")
        if 'failures' not in tables:
            cur.execute("CREATE TABLE failures(id PRIMARY KEY, build_id)")
        cur.close()

    def __del__(self):
        self._db.close()

    def cursor(self):
        # TODO: add internal API
        return self._db.cursor()

    def add_failure(self, failureid: int, buildid: int):
        cur = self._db.cursor()
        cur.execute("INSERT INTO failures(id, build_id)"
                    "VALUES(?, ?) ON CONFLICT(id) "
                    "DO NOTHING;", (failureid, buildid))
        print("INSERT INTO failures(id, build_id)"
                    f"VALUES({failureid}, {buildid}) ON CONFLICT(id) "
                    "DO NOTHING;")
        cur.close()

    def add_build(self, data):
        cur = self._db.cursor()
        try:
            cur.execute("INSERT INTO build VALUES(:id, :status, :test, "
                        ":worker, :completed, :collection_id, :ab_url, :owner, "
                        ":branch, :parent_id);", data)
        except Exception as e:
            print(e)
        cur.close()

    def commit(self):
        self._db.commit()


