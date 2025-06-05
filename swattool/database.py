#!/usr/bin/env python3

"""Interaction with the swatbot Django server.

This module provides functionality for retrieving build failures data from the
swatbot server.
"""

import logging
import sqlite3
from typing import Any, Collection, Optional

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
            cur.execute("CREATE TABLE build(build_id PRIMARY KEY, "
                        "swatbot_build_id, status, test, worker, completed, "
                        "collection_id, ab_url, parent_id)")
        if 'collection' not in tables:
            cur.execute("CREATE TABLE collection(collection_id PRIMARY KEY, "
                        "owner, branch, collection_build_id, target_name, "
                        "parent_builder, parent_build_number)")
        if 'failure' not in tables:
            cur.execute("CREATE TABLE "
                        "failure(failure_id PRIMARY KEY, build_id, "
                        "step_number, step_name, urls, remote_triage, "
                        "remote_triage_notes)")
        cur.close()

    def __del__(self):
        self._db.close()


    def add_failures(self, data):
        cur = self._db.cursor()
        cur.executemany("INSERT INTO failure "
                        "VALUES(:failure_id, :build_id, :step_number, "
                        ":step_name, :urls, :remote_triage, "
                        ":remote_triage_notes) "
                        "ON CONFLICT(failure_id) DO NOTHING;", data)
        cur.close()

    def get_failures(self,
                     triage: set[swatbotrest.TriageStatus] = set(),
                     with_data: Optional[bool] = False,
                     limit: Optional[int] = None):
        cur = self._db.cursor()
        remote_triage = ", ".join({str(int(t)) for t in triage})
        data = {'limit': limit}
        req = "Select * FROM failure "
        if with_data:
            req += "INNER JOIN build ON failure.build_id = build.build_id " \
                "INNER JOIN collection " \
                "ON build.collection_id = collection.collection_id "
        if triage is not None:
            req += f"WHERE failure.remote_triage IN ({remote_triage}) "
        req += "ORDER BY failure.build_id "
        if limit is not None:
            req += "LIMIT ':limit' "

        build_res = cur.execute(req, data)
        return {row['build_id']: row for row in build_res.fetchall()}

    def add_build(self, data):
        cur = self._db.cursor()
        cur.execute("INSERT INTO build VALUES(:build_id, :buildbot_build_id, "
                    ":status, :test, :worker, :completed, :collection_id, "
                    ":ab_url, :parent_id);", data)
        cur.close()

    def get_builds(self):
        cur = self._db.cursor()
        build_res = cur.execute("Select * from build")
        return {row['id']: row for row in build_res.fetchall()}

    def get_builds_ids(self):
        cur = self._db.cursor()
        build_res = cur.execute("Select build_id from build")
        return {row['build_id'] for row in build_res.fetchall()}

    def add_collection(self, data):
        cur = self._db.cursor()
        cur.execute("INSERT INTO collection "
                    "VALUES(:collection_id, :owner, :branch, "
                    ":collection_build_id, :target_name, :parent_builder, "
                    ":parent_build_number)",
                    data)
        cur.close()

    def get_collections_ids(self):
        cur = self._db.cursor()
        build_res = cur.execute("Select collection_id from collection")
        return {row['collection_id'] for row in build_res.fetchall()}

    def commit(self):
        self._db.commit()


