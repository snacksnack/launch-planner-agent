"""Tests for the plan-of-record store: content hashing, the commit gate, lineage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from planner_core import (
    CommitRejected,
    Confidence,
    InMemoryPlanRepository,
    Plan,
    Provenance,
    SnapshotKind,
    Task,
    TeamMember,
    ThreePointEstimate,
    commit_plan,
    content_hash,
    record_proposal,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _prov() -> Provenance:
    return Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=NOW,
    )


def _task(tid: str, owner: str | None) -> Task:
    return Task(
        id=tid, name=tid, owner_id=owner,
        estimate=ThreePointEstimate(optimistic=1, likely=2, pessimistic=3), provenance=_prov(),
    )


def _valid_plan(name: str = "p") -> Plan:
    return Plan(id="p", name=name, team=[TeamMember(id="tm-1", name="Ada")],
                tasks=[_task("a", "tm-1")])


def _invalid_plan() -> Plan:
    # Owner not in the team -> a blocking validation error.
    return Plan(id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")],
                tasks=[_task("a", "ghost")])


# --- content hashing -------------------------------------------------------


def test_content_hash_is_deterministic_and_sensitive():
    assert content_hash(_valid_plan()) == content_hash(_valid_plan())
    assert content_hash(_valid_plan("one")) != content_hash(_valid_plan("two"))


# --- commit gate -----------------------------------------------------------


def test_commit_requires_an_approver():
    repo = InMemoryPlanRepository()
    with pytest.raises(CommitRejected, match="approver"):
        commit_plan(repo, _valid_plan(), approved_by="  ", now=NOW)
    assert repo.history() == []  # nothing written


def test_commit_refuses_a_plan_with_validation_errors():
    repo = InMemoryPlanRepository()
    with pytest.raises(CommitRejected) as exc:
        commit_plan(repo, _invalid_plan(), approved_by="Reid", now=NOW)
    assert exc.value.issues  # carries the blocking issues
    assert repo.history() == []


def test_commit_writes_an_immutable_snapshot():
    repo = InMemoryPlanRepository()
    snap = commit_plan(repo, _valid_plan(), approved_by="Reid", now=NOW, message="ship it")
    assert snap.version == 1
    assert snap.kind is SnapshotKind.COMMIT
    assert snap.approved_by == "Reid"
    assert snap.parent_hash is None
    assert snap.content_hash == content_hash(_valid_plan())
    assert repo.get_by_version(1) == snap
    assert repo.get_by_hash(snap.content_hash) == snap


def test_commits_form_a_parent_linked_chain():
    repo = InMemoryPlanRepository()
    first = commit_plan(repo, _valid_plan("one"), approved_by="Reid", now=NOW)
    second = commit_plan(repo, _valid_plan("two"), approved_by="Reid", now=NOW)
    assert second.version == 2
    assert second.parent_hash == first.content_hash  # event-log lineage
    assert repo.latest_commit() == second


def test_record_proposal_stores_a_proposal_kind():
    repo = InMemoryPlanRepository()
    snap = record_proposal(repo, _valid_plan(), now=NOW)
    assert snap.kind is SnapshotKind.PROPOSAL
    assert repo.latest_commit() is None  # a proposal is not a commit
