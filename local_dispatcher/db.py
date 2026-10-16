"""SQLite access: single WAL connection on the event-loop thread.

All DB access happens in coroutine context on the daemon's event loop (no
aiosqlite, no threads). Queries are fast; a write transaction holding the
loop for a few milliseconds is acceptable at MVP scale. Migrations are
numbered .sql files applied in order, tracked in schema_version.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def now_iso() -> str:
    """UTC timestamp matching the schema's default format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.conn.close()

    # -- migrations ------------------------------------------------------
    def migrate(self) -> int:
        # The version table is created by 0001 itself, so on a brand-new
        # database it does not exist yet — create it if needed first.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "v INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
        applied = {r["v"] for r in self.conn.execute("SELECT v FROM schema_version")}
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for f in files:
            v = int(f.name.split("_", 1)[0])
            if v in applied:
                continue
            # executescript() would commit the open transaction, so run the
            # statements individually inside our own BEGIN IMMEDIATE.
            statements = [s.strip() for s in f.read_text().split(";") if s.strip()]
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for s in statements:
                    self.conn.execute(s)
                self.conn.execute("INSERT INTO schema_version(v) VALUES (?)", (v,))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return max(applied | {int(f.name.split('_', 1)[0]) for f in files})

    # -- transaction helper ----------------------------------------------
    @contextmanager
    def tx(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Context manager: BEGIN IMMEDIATE … COMMIT/ROLLBACK."""
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # -- convenience ------------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def event(self, subject_type: str, subject_id: str, name: str, detail: dict | None = None) -> None:
        import json

        self.conn.execute(
            "INSERT INTO events(subject_type, subject_id, event, detail_json) VALUES (?,?,?,?)",
            (subject_type, subject_id, name, json.dumps(detail or {})),
        )

    def prune_events(self, max_age_days: int = 30, max_rows: int = 50000) -> int:
        row = self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()
        if row["c"] <= max_rows:
            return 0
        self.conn.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
            (max_rows,),
        )
        return row["c"] - max_rows
