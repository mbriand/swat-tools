#!/usr/bin/env python3

"""SQLite database storage for failure information.

This module provides functionality for storing and retrieving build failures data
in a local SQLite database, reducing the need for frequent server requests.
"""

import logging
import sqlite3
from typing import Any, Optional

from . import swatbotrest
from . import utils

logger = logging.getLogger(__name__)


class Database:
    """Swattool data storage database.

    Manages a SQLite database that stores failure information locally.
    This allows for faster access to data and reduced server requests.
    The database contains tables for builds, collections, and failures.
    """

    def __init__(self):
        self._db = sqlite3.connect(utils.DATADIR / "swattool.db")
        self._db.row_factory = sqlite3.Row
        cur = self._db.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cur.fetchall()]

        if 'build' not in tables:
            cur.execute("CREATE TABLE build(build_id PRIMARY KEY, "
                        "buildbot_build_id, status, test, worker, completed, "
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

    def add_failures(self, data: list[dict[str, Any]]) -> None:
        """Add failure entries in the database.

        Args:
            data: A list of dictionaries containing failure data with keys:
                failure_id, build_id, step_number, step_name, urls,
                remote_triage, and remote_triage_notes.
        """
        cur = self._db.cursor()
        cur.executemany("INSERT INTO failure "
                        "VALUES(:failure_id, :build_id, :step_number, "
                        ":step_name, :urls, :remote_triage, "
                        ":remote_triage_notes) "
                        "ON CONFLICT(failure_id) DO NOTHING;", data)
        cur.close()

    def get_failures(self,
                     triage: Optional[set[swatbotrest.TriageStatus]],
                     with_data: Optional[bool] = False,
                     limit: Optional[int] = None
                     ) -> list[sqlite3.Row]:
        """Get failure entries from the database.

        Args:
            triage: Optional filter for specific triage statuses.
            with_data: If True, join with build and collection data.
            limit: Optional maximum number of entries to return.

        Returns:
            Dictionary mapping build_id to row data for matching failures.
        """
        cur = self._db.cursor()
        data = {'limit': limit}
        req = "Select * FROM failure "
        if with_data:
            req += "INNER JOIN build ON failure.build_id = build.build_id " \
                "INNER JOIN collection " \
                "ON build.collection_id = collection.collection_id "
        if triage:
            remote_triage = ", ".join({str(int(t)) for t in triage})
            req += f"WHERE failure.remote_triage IN ({remote_triage}) "
        req += "ORDER BY failure.build_id "
        if limit is not None:
            req += "LIMIT ':limit' "

        build_res = cur.execute(req, data)
        return build_res.fetchall()

    def get_missing_failures(self) -> list[int]:
        """Get ids of failures missing from database but referenced.

        Returns:
            List of build_ids that are referenced in the failure table
            but missing from the build table.
        """
        cur = self._db.cursor()
        req = "Select failure.build_id FROM failure " \
            "LEFT JOIN build ON failure.build_id = build.build_id " \
            "WHERE build.build_id is NULL " \
            "GROUP BY failure.build_id"

        build_res = cur.execute(req)
        return [row['build_id'] for row in build_res.fetchall()]

    def get_missing_collections(self) -> list[tuple[int, str]]:
        """Get ids of collections missing from database but referenced.

        Returns:
            List of tuples containing (collection_id, autobuilder_url) for
            collections that are referenced in the build table but missing
            from the collection table.
        """
        cur = self._db.cursor()
        req = "Select build.collection_id, build.ab_url FROM failure " \
            "LEFT JOIN build ON failure.build_id = build.build_id " \
            "LEFT JOIN collection " \
            "ON build.collection_id = collection.collection_id " \
            "WHERE collection.collection_id is NULL " \
            "AND build.collection_id is NOT NULL " \
            "GROUP BY build.collection_id"

        build_res = cur.execute(req)
        return [(row['collection_id'], row['ab_url'])
                for row in build_res.fetchall()]

    def add_build(self, data: dict[str, Any]) -> None:
        """Add build entry in the database.

        Args:
            data: Dictionary containing build data with keys matching
                the build table schema.
        """
        cur = self._db.cursor()
        cur.execute("INSERT INTO build VALUES(:build_id, :buildbot_build_id, "
                    ":status, :test, :worker, :completed, :collection_id, "
                    ":ab_url, :parent_id);", data)
        cur.close()

    def get_builds(self) -> dict[int, sqlite3.Row]:
        """Get build entries from the database.

        Returns:
            Dictionary mapping build id to row data for all builds.
        """
        cur = self._db.cursor()
        build_res = cur.execute("Select * from build")
        return {row['id']: row for row in build_res.fetchall()}

    def get_builds_ids(self) -> set[int]:
        """Get ids of build entries from the database.

        Returns:
            Set of all build_ids stored in the database.
        """
        cur = self._db.cursor()
        build_res = cur.execute("Select build_id from build")
        return {row['build_id'] for row in build_res.fetchall()}

    def add_collection(self, data: dict[str, Any]) -> None:
        """Add collection entry in the database.

        Args:
            data: Dictionary containing collection data with keys matching
                the collection table schema.
        """
        cur = self._db.cursor()
        cur.execute("INSERT INTO collection "
                    "VALUES(:collection_id, :owner, :branch, "
                    ":collection_build_id, :target_name, :parent_builder, "
                    ":parent_build_number)",
                    data)
        cur.close()

    def get_collections_ids(self) -> set[int]:
        """Get ids of collection entries from the database.

        Returns:
            Set of all collection_ids stored in the database.
        """
        cur = self._db.cursor()
        build_res = cur.execute("Select collection_id from collection")
        return {row['collection_id'] for row in build_res.fetchall()}

    def commit(self) -> None:
        """Commit database changes.

        Ensures all changes are permanently stored in the database file.
        """
        self._db.commit()
