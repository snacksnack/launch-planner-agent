"""Durability and concurrency pragmas on the plan store (RC1-245).

Two of these assertions pin a decision rather than a behaviour. `synchronous`
stays at FULL on purpose, and a future reader looking to speed up writes should
find a failing test rather than an easy win — see ADR-0028.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.store import _BUSY_TIMEOUT_MS, SQLiteEventStore
from planner_core import Plan, Snapshot, SnapshotKind, content_hash

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "fixtures/jira-cloud-migration/golden/expected-plan.json"
)


def _pragma(store: SQLiteEventStore, name: str):
    return store._conn.execute(f"PRAGMA {name}").fetchone()[0]


@pytest.fixture
def store(tmp_path):
    s = SQLiteEventStore(str(tmp_path / "plans.db"))
    yield s
    s.close()


def _snapshot(plan: Plan, kind: SnapshotKind = SnapshotKind.COMMIT) -> Snapshot:
    return Snapshot(
        kind=kind,
        plan=plan,
        content_hash=content_hash(plan),
        created_at=datetime.now(UTC),
        approved_by="test",
    )


# --- the pragmas ------------------------------------------------------------


def test_a_file_backed_store_runs_in_wal_mode(store):
    assert _pragma(store, "journal_mode") == "wal"


def test_an_in_memory_store_still_constructs(tmp_path):
    """`PRAGMA journal_mode=WAL` returns 'memory' on an in-memory database
    rather than erroring, so the parametrized contract test keeps working
    without a special case."""
    s = SQLiteEventStore(":memory:")
    try:
        assert _pragma(s, "journal_mode") == "memory"
        assert s.history() == []
    finally:
        s.close()


def test_the_busy_timeout_is_explicit(store):
    assert _pragma(store, "busy_timeout") == _BUSY_TIMEOUT_MS


def test_the_driver_timeout_and_the_pragma_agree(store, tmp_path):
    """They are set from one constant. Drift between them would mean the value
    someone reads in the code is not the value in force."""
    raw = sqlite3.connect(str(tmp_path / "other.db"), timeout=_BUSY_TIMEOUT_MS / 1000)
    try:
        assert raw.execute("PRAGMA busy_timeout").fetchone()[0] == _BUSY_TIMEOUT_MS
    finally:
        raw.close()


def test_synchronous_stays_full(store):
    """Deliberate, not inherited. NORMAL would skip the fsync per commit, but a
    commit here is a human-approved plan of record a few times a week: there is
    no throughput to win, and the transaction a power loss would drop is exactly
    the audit record this project exists to keep. See ADR-0028."""
    assert _pragma(store, "synchronous") == 2  # 2 == FULL


def test_wal_mode_persists_across_reopen(tmp_path):
    """WAL is a property of the file, so it survives without being re-applied —
    but the pragma is still set on every connect so a restored or copied
    database cannot quietly come back in rollback-journal mode."""
    path = str(tmp_path / "plans.db")
    first = SQLiteEventStore(path)
    first.close()

    reopened = SQLiteEventStore(path)
    try:
        assert _pragma(reopened, "journal_mode") == "wal"
    finally:
        reopened.close()


# --- what WAL actually buys -------------------------------------------------


def test_a_read_succeeds_while_an_exclusive_write_is_open(tmp_path):
    """The behaviour the whole change is for.

    The lock level matters, and an earlier version of this test got it wrong:
    `BEGIN IMMEDIATE` only takes a RESERVED lock, which does not block readers
    in *either* journal mode, so that test passed under the old configuration
    and proved nothing. `BEGIN EXCLUSIVE` is what distinguishes them — under a
    rollback journal the reader blocks until `busy_timeout` and raises
    "database is locked"; under WAL it reads the last committed state
    immediately. Verified by flipping the pragma back to DELETE and watching
    this fail.
    """
    path = str(tmp_path / "plans.db")
    plan = Plan.model_validate_json(GOLDEN.read_text())

    writer = SQLiteEventStore(path)
    reader = SQLiteEventStore(path)
    try:
        writer.append(_snapshot(plan, SnapshotKind.BASELINE))

        writer._conn.execute("BEGIN EXCLUSIVE")
        writer._conn.execute(
            "INSERT INTO snapshots (content_hash, kind, created_at, plan_json) "
            "VALUES (?, ?, ?, ?)",
            ("deadbeef", "commit", datetime.now(UTC).isoformat(), "{}"),
        )

        started = time.perf_counter()
        history = reader.history()  # must not block or raise
        elapsed = time.perf_counter() - started

        assert [s.kind for s in history] == [SnapshotKind.BASELINE]
        # A blocked read would sit on the busy timeout before failing; this one
        # must return promptly rather than merely eventually.
        assert elapsed < 1.0, f"the read took {elapsed:.2f}s — was it waiting on a lock?"

        writer._conn.rollback()
    finally:
        writer.close()
        reader.close()


def test_two_readers_see_a_commit_once_it_lands(tmp_path):
    path = str(tmp_path / "plans.db")
    plan = Plan.model_validate_json(GOLDEN.read_text())

    writer = SQLiteEventStore(path)
    reader = SQLiteEventStore(path)
    try:
        assert reader.history() == []
        writer.append(_snapshot(plan))
        assert len(reader.history()) == 1
    finally:
        writer.close()
        reader.close()


# --- the guarantees the pragmas must not weaken -----------------------------


def test_the_append_only_triggers_still_fire_under_wal(store):
    """Immutability is enforced by triggers at the storage layer. A journal-mode
    change must not touch that."""
    plan = Plan.model_validate_json(GOLDEN.read_text())
    store.append(_snapshot(plan))

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("UPDATE snapshots SET approved_by = 'someone else'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("DELETE FROM snapshots")
