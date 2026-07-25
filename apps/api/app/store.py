"""SQLite adapter for the plan-of-record store (the `PlanRepository` port).

Append-only by construction: the schema installs triggers that *reject* UPDATE
and DELETE, so immutability is enforced at the storage layer, not merely by
convention. The `app` layer owns the DB connection; `planner_core` owns the port
and domain. A Postgres adapter can implement the same port for deployment.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from planner_core import DecisionRecord, Plan, Snapshot, SnapshotKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    version              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash         TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    parent_hash          TEXT,
    source_proposal_hash TEXT,
    approved_by          TEXT,
    message              TEXT,
    created_at           TEXT NOT NULL,
    plan_json            TEXT NOT NULL,
    decision_json        TEXT
);

-- Immutability enforced by the database, not just the application.
CREATE TRIGGER IF NOT EXISTS snapshots_no_update
    BEFORE UPDATE ON snapshots
    BEGIN SELECT RAISE(FAIL, 'snapshots are append-only'); END;

CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
    BEFORE DELETE ON snapshots
    BEGIN SELECT RAISE(FAIL, 'snapshots are append-only'); END;
"""

_COLUMNS = (
    "version, content_hash, kind, parent_hash, source_proposal_hash, "
    "approved_by, message, created_at, plan_json, decision_json"
)


class SQLiteEventStore:
    """An append-only `PlanRepository` backed by a SQLite file (or `:memory:`)."""

    def __init__(self, path: str) -> None:
        self._path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # A shared connection so an in-memory DB persists across calls.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (append-only-safe:
        ADD COLUMN neither updates nor deletes rows, so the triggers don't fire)."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(snapshots)")}
        if "decision_json" not in existing:
            self._conn.execute("ALTER TABLE snapshots ADD COLUMN decision_json TEXT")

    def close(self) -> None:
        self._conn.close()

    def append(self, snapshot: Snapshot) -> Snapshot:
        decision_json = (
            snapshot.decision_record.model_dump_json() if snapshot.decision_record else None
        )
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO snapshots "
                "(content_hash, kind, parent_hash, source_proposal_hash, "
                " approved_by, message, created_at, plan_json, decision_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.content_hash,
                    snapshot.kind.value,
                    snapshot.parent_hash,
                    snapshot.source_proposal_hash,
                    snapshot.approved_by,
                    snapshot.message,
                    snapshot.created_at.isoformat(),
                    snapshot.plan.model_dump_json(),
                    decision_json,
                ),
            )
        return snapshot.model_copy(update={"version": cursor.lastrowid})

    def get_by_version(self, version: int) -> Snapshot | None:
        row = self._one("SELECT " + _COLUMNS + " FROM snapshots WHERE version = ?", (version,))
        return _row_to_snapshot(row) if row else None

    def get_by_hash(self, content_hash: str) -> Snapshot | None:
        row = self._one(
            "SELECT " + _COLUMNS + " FROM snapshots WHERE content_hash = ? "
            "ORDER BY version DESC LIMIT 1",
            (content_hash,),
        )
        return _row_to_snapshot(row) if row else None

    def history(self) -> list[Snapshot]:
        sql = "SELECT " + _COLUMNS + " FROM snapshots ORDER BY version"
        with closing(self._conn.execute(sql)) as cur:
            return [_row_to_snapshot(row) for row in cur.fetchall()]

    def latest_commit(self) -> Snapshot | None:
        row = self._one(
            "SELECT " + _COLUMNS + " FROM snapshots WHERE kind = ? "
            "ORDER BY version DESC LIMIT 1",
            (SnapshotKind.COMMIT.value,),
        )
        return _row_to_snapshot(row) if row else None

    def _one(self, sql: str, params: tuple) -> sqlite3.Row | None:
        with closing(self._conn.execute(sql, params)) as cursor:
            return cursor.fetchone()


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    decision_json = row["decision_json"]
    return Snapshot(
        version=row["version"],
        content_hash=row["content_hash"],
        kind=SnapshotKind(row["kind"]),
        plan=Plan.model_validate_json(row["plan_json"]),
        parent_hash=row["parent_hash"],
        source_proposal_hash=row["source_proposal_hash"],
        approved_by=row["approved_by"],
        message=row["message"],
        created_at=datetime.fromisoformat(row["created_at"]),
        decision_record=(
            DecisionRecord.model_validate_json(decision_json) if decision_json else None
        ),
    )
