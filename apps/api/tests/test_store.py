"""Tests for the SQLite plan store: enforced immutability + the port contract.

The `test_repository_contract_*` tests run against BOTH the in-memory reference
repo and the SQLite adapter via one parametrized fixture — that's the payoff of
the repository port: the same contract holds regardless of the backend.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from app.store import SQLiteEventStore
from planner_core import (
    Confidence,
    InMemoryPlanRepository,
    Plan,
    Provenance,
    SnapshotKind,
    Task,
    TeamMember,
    ThreePointEstimate,
    commit_plan,
    record_proposal,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _valid_plan(name: str = "p") -> Plan:
    prov = Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=NOW,
    )
    return Plan(
        id="p", name=name, team=[TeamMember(id="tm-1", name="Ada")],
        tasks=[Task(
            id="a", name="a", owner_id="tm-1",
            estimate=ThreePointEstimate(optimistic=1, likely=2, pessimistic=3), provenance=prov,
        )],
    )


@pytest.fixture(params=["in-memory", "sqlite"])
def repo(request):
    if request.param == "in-memory":
        yield InMemoryPlanRepository()
    else:
        store = SQLiteEventStore(":memory:")
        yield store
        store.close()


# --- port contract (runs against both backends) ----------------------------


def test_repository_contract_append_and_retrieve(repo):
    proposal = record_proposal(repo, _valid_plan("proposed"), now=NOW)
    commit = commit_plan(repo, _valid_plan("committed"), approved_by="Reid", now=NOW, message="go")

    assert proposal.version == 1 and commit.version == 2
    assert repo.get_by_version(2) == commit
    assert repo.get_by_hash(commit.content_hash).plan.name == "committed"
    assert [s.kind for s in repo.history()] == [SnapshotKind.PROPOSAL, SnapshotKind.COMMIT]
    assert repo.latest_commit() == commit


def test_repository_contract_round_trips_the_plan(repo):
    committed = commit_plan(repo, _valid_plan(), approved_by="Reid", now=NOW)
    fetched = repo.get_by_version(committed.version)
    assert fetched.plan == _valid_plan()  # full Plan survives the round trip


# --- SQLite-specific: immutability enforced by the database ----------------


def test_sqlite_rejects_update_and_delete(tmp_path):
    store = SQLiteEventStore(str(tmp_path / "plans.db"))
    try:
        commit_plan(store, _valid_plan(), approved_by="Reid", now=NOW)
        with pytest.raises(sqlite3.Error, match="append-only"):
            store._conn.execute("UPDATE snapshots SET message = 'tamper' WHERE version = 1")
        with pytest.raises(sqlite3.Error, match="append-only"):
            store._conn.execute("DELETE FROM snapshots WHERE version = 1")
    finally:
        store.close()


def test_sqlite_persists_across_store_instances(tmp_path):
    path = str(tmp_path / "plans.db")
    store = SQLiteEventStore(path)
    commit_plan(store, _valid_plan(), approved_by="Reid", now=NOW)
    store.close()

    reopened = SQLiteEventStore(path)
    try:
        assert len(reopened.history()) == 1
        assert reopened.latest_commit().approved_by == "Reid"
    finally:
        reopened.close()
