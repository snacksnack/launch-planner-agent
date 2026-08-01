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
    CommitRejected,
    Confidence,
    DelayTask,
    InMemoryPlanRepository,
    Plan,
    Provenance,
    SavedScenario,
    Scenario,
    SnapshotKind,
    Task,
    TeamMember,
    ThreePointEstimate,
    commit_baseline,
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


def test_repository_contract_persists_the_decision_record(repo):
    """The build-time audit rides the immutable snapshot (RC1-197)."""
    from planner_core import build_decision_record

    plan = _valid_plan()
    record = build_decision_record(plan, "unrelated prd text")  # flags the unverifiable quote
    committed = commit_plan(repo, plan, approved_by="Reid", now=NOW, decision_record=record)

    fetched = repo.get_by_version(committed.version)
    assert fetched.decision_record == record
    assert any(f.code == "unverifiable-quote" for f in fetched.decision_record.flagged)


def test_repository_contract_decision_record_is_optional(repo):
    committed = commit_plan(repo, _valid_plan(), approved_by="Reid", now=NOW)
    assert repo.get_by_version(committed.version).decision_record is None


# --- baselines (RC1-192) ---------------------------------------------------


def test_baseline_is_recorded_and_is_the_latest_of_record(repo):
    commit_plan(repo, _valid_plan("v1"), approved_by="Reid", now=NOW)
    base = commit_baseline(repo, _valid_plan("v2"), approved_by="Reid", note="initial", now=NOW)

    assert base.kind is SnapshotKind.BASELINE
    assert base.message == "initial"
    assert repo.latest_baseline().version == base.version
    assert repo.latest_of_record().version == base.version  # a baseline is a record
    # A later ordinary commit becomes the newest record but not the baseline.
    later = commit_plan(repo, _valid_plan("v3"), approved_by="Reid", now=NOW)
    assert repo.latest_of_record().version == later.version
    assert repo.latest_baseline().version == base.version


def test_rebaseline_takes_the_latest_baseline(repo):
    first = commit_baseline(repo, _valid_plan("a"), approved_by="R", note="first", now=NOW)
    second = commit_baseline(repo, _valid_plan("b"), approved_by="R", note="re-baseline", now=NOW)
    assert repo.latest_baseline().version == second.version
    assert first.version != second.version


def test_baseline_requires_a_note(repo):
    with pytest.raises(CommitRejected, match="note"):
        commit_baseline(repo, _valid_plan(), approved_by="Reid", note="  ", now=NOW)


def test_baseline_still_gated_on_validation_and_approver(repo):
    with pytest.raises(CommitRejected, match="approver"):
        commit_baseline(repo, _valid_plan(), approved_by="", note="x", now=NOW)


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


# --- saved what-if scenarios (RC1-202) -------------------------------------


def _saved(name: str, plan_hash: str, days: float = 5, **kw) -> SavedScenario:
    return SavedScenario(
        name=name, plan_hash=plan_hash,
        scenario=Scenario(name=name, changes=[DelayTask(task_id="a", days=days)]),
        created_at=NOW, **kw,
    )


def test_scenario_save_get_and_list_by_plan_hash():
    store = SQLiteEventStore(":memory:")
    try:
        store.save_scenario(_saved("aggressive", "hashA", created_by="Priya", note="worst case"))
        store.save_scenario(_saved("mild", "hashA"))
        store.save_scenario(_saved("other-plan", "hashB"))

        got = store.get_scenario("aggressive", "hashA")
        assert got.created_by == "Priya" and got.note == "worst case"
        assert got.scenario.changes[0].task_id == "a"

        names = {s.name for s in store.list_scenarios("hashA")}
        assert names == {"aggressive", "mild"}  # scoped to the plan hash
        assert [s.name for s in store.list_scenarios("hashB")] == ["other-plan"]
        assert len(store.list_scenarios()) == 3  # all, unfiltered
    finally:
        store.close()


def test_scenario_save_overwrites_by_plan_and_name():
    store = SQLiteEventStore(":memory:")
    try:
        store.save_scenario(_saved("s", "hashA", days=5, note="first"))
        store.save_scenario(_saved("s", "hashA", days=9, note="second"))
        rows = store.list_scenarios("hashA")
        assert len(rows) == 1  # overwritten, not duplicated
        assert rows[0].note == "second"
        assert rows[0].scenario.changes[0].days == 9
    finally:
        store.close()


def test_scenario_delete_reports_whether_it_removed_one():
    store = SQLiteEventStore(":memory:")
    try:
        store.save_scenario(_saved("s", "hashA"))
        assert store.delete_scenario("s", "hashA") is True
        assert store.get_scenario("s", "hashA") is None
        assert store.delete_scenario("s", "hashA") is False  # already gone
    finally:
        store.close()


def test_scenarios_survive_reopen_and_are_unaffected_by_snapshot_triggers(tmp_path):
    path = str(tmp_path / "plans.db")
    store = SQLiteEventStore(path)
    store.save_scenario(_saved("s", "hashA", note="keep me"))
    store.close()

    reopened = SQLiteEventStore(path)
    try:
        # The append-only triggers are on `snapshots`; the scenarios catalog is mutable.
        assert reopened.get_scenario("s", "hashA").note == "keep me"
        assert reopened.delete_scenario("s", "hashA") is True
    finally:
        reopened.close()
