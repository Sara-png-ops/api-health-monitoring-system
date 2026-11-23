"""SQLite storage for endpoints and their real check results.

A new connection is opened for every operation, which keeps the class safe to
use from Streamlit's script threads. Deleting an endpoint also deletes its
check history (ON DELETE CASCADE).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "monitoring.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id       INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    checked_at        TEXT NOT NULL,
    status_code       INTEGER,
    response_time_ms  REAL,
    result            TEXT NOT NULL CHECK (result IN ('healthy', 'unhealthy', 'error')),
    error_message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_endpoint_time ON checks (endpoint_id, checked_at);
"""

_HISTORY_SELECT = """
SELECT c.id AS check_id, c.endpoint_id, e.name, e.url, c.checked_at,
       c.status_code, c.response_time_ms, c.result, c.error_message
FROM checks c JOIN endpoints e ON e.id = c.endpoint_id
"""


class DuplicateEndpointError(Exception):
    """An endpoint with the same URL is already registered."""


class Database:
    def __init__(self, path=DEFAULT_DB_PATH):
        self.path = str(path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- endpoints ----------------------------------------------------------
    def add_endpoint(self, name: str, url: str) -> int:
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO endpoints (name, url, created_at) VALUES (?, ?, ?)",
                    (name, url, created),
                )
                return cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise DuplicateEndpointError(f"{url} is already being monitored.") from exc

    def remove_endpoint(self, endpoint_id: int) -> bool:
        """Delete an endpoint and its history. Returns False if it did not exist."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
            return cursor.rowcount > 0

    def list_endpoints(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, url, created_at FROM endpoints ORDER BY name COLLATE NOCASE, id"
            ).fetchall()
        return [dict(row) for row in rows]

    # -- checks -------------------------------------------------------------
    def save_check(self, endpoint_id: int, checked_at: str, status_code: Optional[int],
                   response_time_ms: Optional[float], result: str,
                   error_message: Optional[str] = None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO checks (endpoint_id, checked_at, status_code, response_time_ms,"
                " result, error_message) VALUES (?, ?, ?, ?, ?, ?)",
                (endpoint_id, checked_at, status_code, response_time_ms, result, error_message),
            )
            return cursor.lastrowid

    def get_history(self, endpoint_id: Optional[int] = None,
                    limit: Optional[int] = None) -> list[dict]:
        """Stored checks, newest first, optionally for one endpoint."""
        query, params = _HISTORY_SELECT, []
        if endpoint_id is not None:
            query += " WHERE c.endpoint_id = ?"
            params.append(endpoint_id)
        query += " ORDER BY c.checked_at DESC, c.id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_latest_checks(self) -> list[dict]:
        """Each endpoint with its most recent check (check columns are None if never checked)."""
        query = """
        SELECT e.id AS endpoint_id, e.name, e.url, c.id AS check_id, c.checked_at,
               c.status_code, c.response_time_ms, c.result, c.error_message
        FROM endpoints e
        LEFT JOIN checks c ON c.id = (
            SELECT id FROM checks WHERE endpoint_id = e.id
            ORDER BY checked_at DESC, id DESC LIMIT 1
        )
        ORDER BY e.name COLLATE NOCASE, e.id
        """
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query).fetchall()]

    def count_checks(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
