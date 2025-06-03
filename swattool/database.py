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
        self._db.row_factory = sqlite3.Row
        cur = self._db.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cur.fetchall()]

        if 'build' not in tables:
            cur.execute("CREATE TABLE build(id PRIMARY KEY, buildid, status, "
                        "test, worker, completed, collection_id, ab_url, "
                        "parent_id)")
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

    def add_failures(self, data):
        cur = self._db.cursor()
        cur.executemany("INSERT INTO failures VALUES(:id, :build_id)"
                        "ON CONFLICT(id) DO NOTHING;", data)
        cur.close()

    def add_build(self, data):
        cur = self._db.cursor()
        cur.execute("INSERT INTO build VALUES(:id, :buildid, :status, :test, "
                    ":worker, :completed, :collection_id, :ab_url, "
                    ":parent_id);", data)
        cur.close()

    def get_builds(self):
        cur = self._db.cursor()
        build_res = cur.execute("Select * from build")
        return {row['id']: row for row in build_res.fetchall()}

    def get_builds_ids(self):
        cur = self._db.cursor()
        build_res = cur.execute("Select id from build")
        return {row['id'] for row in build_res.fetchall()}

    def add_collection(self, data):
        cur = self._db.cursor()
        cur.execute("INSERT INTO collection "
                    "VALUES(:id, :owner, :branch, :build_id)",
                    data)
        cur.close()

    def get_collections_ids(self):
        cur = self._db.cursor()
        build_res = cur.execute("Select id from collection")
        return {row['id'] for row in build_res.fetchall()}

    def commit(self):
        self._db.commit()


