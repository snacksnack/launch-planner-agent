"""Immutable plan-of-record store: the "human approves" leg, event-sourced.

Nothing becomes a plan of record until a person reviews and commits it. A commit
is an **append-only, content-addressed** snapshot: the plan is serialized
canonically and hashed (sha256), snapshots are never mutated, and each links to
its parent — an event log of the plan's history. Committing is gated: a plan with
validation errors (unknown owner, dangling/cyclic dependency) cannot be committed,
and an explicit approver is required.

This module defines the domain (`Snapshot`), the storage **port**
(`PlanRepository`, a Protocol), the content hash, and the commit service. The
concrete SQLite adapter lives in the `app` layer (which owns the DB connection);
`InMemoryPlanRepository` here is a reference implementation and test double. A
Postgres adapter can drop in behind the same port for deployment.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from planner_core.decision_record import DecisionRecord
from planner_core.dependencies import build_dependency_report
from planner_core.models import Plan
from planner_core.raid_analysis import build_raid_report
from planner_core.validation import ValidationIssue, build_report


class SnapshotKind(StrEnum):
    PROPOSAL = "proposal"  # an agent's original output, stored for the audit diff
    COMMIT = "commit"  # a human-reviewed, approved plan of record
    BASELINE = "baseline"  # a commit designated as a measurement reference (RC1-192)


# The snapshot kinds that are a plan of record (measurable versions), newest first.
_RECORD_KINDS = (SnapshotKind.COMMIT, SnapshotKind.BASELINE)


class Snapshot(BaseModel):
    """One immutable entry in the plan history."""

    model_config = ConfigDict(extra="forbid")

    version: int | None = None  # assigned by the store on append (1-based)
    content_hash: str
    kind: SnapshotKind
    plan: Plan
    parent_hash: str | None = None
    source_proposal_hash: str | None = None  # for a commit: the proposal it derives from
    approved_by: str | None = None
    message: str | None = None
    created_at: datetime
    # The audit of how this plan was built (RC1-197). Metadata *about* the plan,
    # not part of it — kept off the plan so `content_hash` stays clean.
    decision_record: DecisionRecord | None = None


def content_hash(plan: Plan) -> str:
    """Deterministic sha256 of a plan's canonical JSON (content-addressing)."""
    payload = plan.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@runtime_checkable
class PlanRepository(Protocol):
    """Append-only store of plan snapshots. Implementations never update/delete."""

    def append(self, snapshot: Snapshot) -> Snapshot:
        """Persist a snapshot and return it with its assigned version."""
        ...

    def get_by_version(self, version: int) -> Snapshot | None: ...

    def get_by_hash(self, content_hash: str) -> Snapshot | None: ...

    def history(self) -> list[Snapshot]:
        """All snapshots in insertion order."""
        ...

    def latest_commit(self) -> Snapshot | None: ...

    def latest_baseline(self) -> Snapshot | None:
        """The most recent snapshot designated as a baseline (RC1-192)."""
        ...

    def latest_of_record(self) -> Snapshot | None:
        """The most recent plan-of-record snapshot (a commit or a baseline)."""
        ...


class InMemoryPlanRepository:
    """Reference implementation + test double for `PlanRepository`."""

    def __init__(self) -> None:
        self._snapshots: list[Snapshot] = []

    def append(self, snapshot: Snapshot) -> Snapshot:
        stored = snapshot.model_copy(update={"version": len(self._snapshots) + 1})
        self._snapshots.append(stored)
        return stored

    def get_by_version(self, version: int) -> Snapshot | None:
        return next((s for s in self._snapshots if s.version == version), None)

    def get_by_hash(self, content_hash: str) -> Snapshot | None:
        # Latest wins if the same content was committed more than once.
        return next(
            (s for s in reversed(self._snapshots) if s.content_hash == content_hash), None
        )

    def history(self) -> list[Snapshot]:
        return list(self._snapshots)

    def latest_commit(self) -> Snapshot | None:
        return next(
            (s for s in reversed(self._snapshots) if s.kind is SnapshotKind.COMMIT), None
        )

    def latest_baseline(self) -> Snapshot | None:
        return next(
            (s for s in reversed(self._snapshots) if s.kind is SnapshotKind.BASELINE), None
        )

    def latest_of_record(self) -> Snapshot | None:
        return next((s for s in reversed(self._snapshots) if s.kind in _RECORD_KINDS), None)


class CommitRejected(Exception):
    """Raised when a plan fails the commit gate (validation errors or no approver)."""

    def __init__(self, reason: str, issues: list[ValidationIssue] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.issues = issues or []


def blocking_errors(plan: Plan) -> list[ValidationIssue]:
    """Error-level issues that must be clear before a plan can be committed.

    Runs every validator for its errors — unknown owners/epics, dangling or
    cyclic dependencies, and RAID errors (unknown owner, duplicate id). Warnings
    (low confidence, coverage gaps) do not block.
    """
    return (
        build_report(plan, "").errors
        + build_dependency_report(plan, "").errors
        + build_raid_report(plan, "").errors
    )


def record_proposal(
    repo: PlanRepository,
    plan: Plan,
    *,
    now: datetime,
    message: str | None = None,
    decision_record: DecisionRecord | None = None,
) -> Snapshot:
    """Store an agent's proposal so a later commit can be diffed against it."""
    snapshot = Snapshot(
        content_hash=content_hash(plan),
        kind=SnapshotKind.PROPOSAL,
        plan=plan,
        message=message,
        created_at=now,
        decision_record=decision_record,
    )
    return repo.append(snapshot)


def commit_plan(
    repo: PlanRepository,
    plan: Plan,
    *,
    approved_by: str,
    now: datetime,
    message: str | None = None,
    source_proposal_hash: str | None = None,
    decision_record: DecisionRecord | None = None,
) -> Snapshot:
    """Gate, then append an immutable plan-of-record snapshot.

    Refuses to commit a plan with validation errors, or without an approver — the
    human-approval leg of "LLM proposes, Python validates, human approves". Links
    the new commit to the previous one (parent), forming the event log. The
    `decision_record`, when supplied, freezes the build-time audit (rejected and
    cycle-broken edges) onto the immutable snapshot.
    """
    _gate(plan, approved_by)
    parent = repo.latest_of_record()
    snapshot = Snapshot(
        content_hash=content_hash(plan),
        kind=SnapshotKind.COMMIT,
        plan=plan,
        parent_hash=parent.content_hash if parent else None,
        source_proposal_hash=source_proposal_hash,
        approved_by=approved_by.strip(),
        message=message,
        created_at=now,
        decision_record=decision_record,
    )
    return repo.append(snapshot)


def _gate(plan: Plan, approved_by: str) -> None:
    """Shared commit gate: an approver is required and the plan must validate."""
    if not approved_by or not approved_by.strip():
        raise CommitRejected("an approver is required to commit a plan of record")
    errors = blocking_errors(plan)
    if errors:
        raise CommitRejected(
            f"plan has {len(errors)} validation error(s); fix them before committing", errors
        )


def commit_baseline(
    repo: PlanRepository,
    plan: Plan,
    *,
    approved_by: str,
    note: str,
    now: datetime,
    decision_record: DecisionRecord | None = None,
) -> Snapshot:
    """Commit a plan and designate it a **baseline** — the reference to measure drift
    against (RC1-192). Like `commit_plan` but requires a `note` (why this baseline,
    e.g. "initial plan" or "re-baseline after approved scope change"). Re-baselining
    is simply appending another baseline; the latest one wins.
    """
    _gate(plan, approved_by)
    if not note or not note.strip():
        raise CommitRejected("a baseline requires a note (why it is being set)")

    parent = repo.latest_of_record()
    snapshot = Snapshot(
        content_hash=content_hash(plan),
        kind=SnapshotKind.BASELINE,
        plan=plan,
        parent_hash=parent.content_hash if parent else None,
        approved_by=approved_by.strip(),
        message=note.strip(),
        created_at=now,
        decision_record=decision_record,
    )
    return repo.append(snapshot)
