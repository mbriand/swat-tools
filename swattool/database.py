#!/usr/bin/env python3

"""SQLite database storage for failure information.

This module provides functionality for storing and retrieving build failures
data in a local SQLite database, reducing the need for frequent server
requests.
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Generator, Optional

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
        utils.DATADIR.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(utils.DATADIR / "swattool.db")
        self._db.row_factory = sqlite3.Row
        self._initialize_tables()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._db.commit()
        else:
            self._db.rollback()
        self._db.close()

    def close(self):
        """Explicitly close the database connection."""
        self._db.close()

    @contextmanager
    def cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for database cursors."""
        cur = self._db.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def _initialize_tables(self):
        """Initialize database tables if they don't exist."""
        with self.cursor() as cur:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cur.fetchall()]

            if 'build' not in tables:
                cur.execute("CREATE TABLE build(build_id PRIMARY KEY, "
                            "buildbot_build_id, status, test, worker, "
                            "completed, collection_id, ab_url, parent_id)")
            if 'collection' not in tables:
                cur.execute("CREATE TABLE collection("
                            "collection_id PRIMARY KEY, owner, branch, "
                            "collection_build_id, target_name, "
                            "parent_builder, parent_build_number, "
                            "yp_build_revision)")
            if 'failure' not in tables:
                cur.execute("CREATE TABLE "
                            "failure(failure_id PRIMARY KEY, build_id, "
                            "step_number, step_name, urls, failure_status, "
                            "remote_triage, remote_triage_notes)")
            if 'logs_data' not in tables:
                cur.execute("CREATE TABLE "
                            "logs_data(ab_instance, logid, build_id, "
                            "step_number, logname, num_lines)")

    def add_failures(self, data: list[dict[str, Any]]) -> None:
        """Add failure entries in the database.

        Args:
            data: A list of dictionaries containing failure data with keys:
                failure_id, build_id, step_number, step_name, urls,
                remote_triage, and remote_triage_notes.
        """
        with self.cursor() as cur:
            cur.executemany("INSERT INTO failure "
                            "VALUES(:failure_id, :build_id, :step_number, "
                            ":step_name, :urls, :failure_status, "
                            ":remote_triage, :remote_triage_notes) "
                            "ON CONFLICT(failure_id) DO NOTHING;", data)

    def drop_failures(self,
                      triage: Optional[swatbotrest.TriageStatus]) -> None:
        """Drop failure entries from the database.

        Args:
            triage: Optional triage status to filter which failures to drop.
                   If None, all failures are dropped.
        """
        with self.cursor() as cur:
            req = "DELETE FROM failure "
            params = []
            if triage:
                req += "WHERE failure.remote_triage = ? "
                params.append(int(triage))

            cur.execute(req, params)

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
        with self.cursor() as cur:
            params = []
            req = "Select * FROM failure "
            if with_data:
                req += "LEFT JOIN build " \
                    "ON failure.build_id = build.build_id " \
                    "LEFT JOIN collection " \
                    "ON build.collection_id = collection.collection_id "
            if triage:
                placeholders = ", ".join("?" * len(triage))
                req += f"WHERE failure.remote_triage IN ({placeholders}) "
                params.extend([int(t) for t in triage])
            req += "ORDER BY failure.build_id "
            if limit is not None:
                req += "LIMIT ? "
                params.append(limit)

            build_res = cur.execute(req, params)
            return build_res.fetchall()

    def get_missing_failures(self) -> list[int]:
        """Get ids of failures missing from database but referenced.

        Returns:
            List of build_ids that are referenced in the failure table
            but missing from the build table.
        """
        with self.cursor() as cur:
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
        with self.cursor() as cur:
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
        with self.cursor() as cur:
            cur.execute("INSERT INTO build VALUES(:build_id, "
                        ":buildbot_build_id, :status, :test, :worker, "
                        ":completed, :collection_id, :ab_url, :parent_id);",
                        data)

    def get_builds(self) -> dict[int, sqlite3.Row]:
        """Get build entries from the database.

        Returns:
            Dictionary mapping build id to row data for all builds.
        """
        with self.cursor() as cur:
            build_res = cur.execute("Select * from build")
            return {row['build_id']: row for row in build_res.fetchall()}

    def get_builds_ids(self) -> set[int]:
        """Get ids of build entries from the database.

        Returns:
            Set of all build_ids stored in the database.
        """
        with self.cursor() as cur:
            build_res = cur.execute("Select build_id from build")
            return {row['build_id'] for row in build_res.fetchall()}

    def add_collection(self, data: dict[str, Any]) -> None:
        """Add collection entry in the database.

        Args:
            data: Dictionary containing collection data with keys matching
                the collection table schema.
        """
        with self.cursor() as cur:
            cur.execute("INSERT INTO collection "
                        "VALUES(:collection_id, :owner, :branch, "
                        ":collection_build_id, :target_name, :parent_builder, "
                        ":parent_build_number, :yp_build_revision)",
                        data)

    def get_collections_ids(self) -> set[int]:
        """Get ids of collection entries from the database.

        Returns:
            Set of all collection_ids stored in the database.
        """
        with self.cursor() as cur:
            build_res = cur.execute("Select collection_id from collection")
            return {row['collection_id'] for row in build_res.fetchall()}

    def get_logs_data(self, build_ids: set[int]) -> list[sqlite3.Row]:
        """Get logs metadata entries from the database.

        Args:
            build_ids: Set of build IDs to retrieve log data for

        Returns:
            List of database rows containing log metadata
        """
        if not build_ids:
            return []

        with self.cursor() as cur:
            placeholders = ", ".join("?" * len(build_ids))
            params = list(build_ids)
            req = "Select * FROM logs_data " \
                f"WHERE logs_data.build_id IN ({placeholders})"

            build_res = cur.execute(req, params)
            return build_res.fetchall()

    def add_logs_data(self, data: list[dict[str, Any]]) -> None:
        """Add logs metadata entries in the database.

        Args:
            data: List of dictionaries containing log metadata to insert
        """
        with self.cursor() as cur:
            cur.executemany("INSERT OR REPLACE INTO logs_data "
                            "VALUES(:ab_instance, :logid, :build_id, "
                            ":step_number, :logname, :num_lines);", data)

    def commit(self) -> None:
        """Commit database changes.

        Ensures all changes are permanently stored in the database file.
        """
        self._db.commit()
