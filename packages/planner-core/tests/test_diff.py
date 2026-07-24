"""Tests for the human-vs-agent plan diff."""

from __future__ import annotations

from datetime import UTC, datetime

from planner_core import (
    Confidence,
    Dependency,
    Provenance,
    Task,
    ThreePointEstimate,
    diff_plans,
)
from planner_core.models import Plan


def _prov() -> Provenance:
    return Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=datetime(2026, 7, 24, tzinfo=UTC),
    )


def _task(tid: str, *, owner: str | None = "tm-1", likely: float = 5) -> Task:
    return Task(
        id=tid, name=tid, owner_id=owner,
        estimate=ThreePointEstimate(optimistic=1, likely=likely, pessimistic=9),
        provenance=_prov(),
    )


def _dep(pred: str, succ: str, lag: float = 0.0) -> Dependency:
    return Dependency(
        id=f"{pred}-{succ}", predecessor_id=pred, successor_id=succ, lag=lag, provenance=_prov()
    )


def _plan(tasks, deps=None) -> Plan:
    return Plan(id="p", name="p", tasks=tasks, dependencies=deps or [])


def test_identical_plans_have_empty_diff():
    plan = _plan([_task("a"), _task("b")], [_dep("a", "b")])
    d = diff_plans(plan, plan)
    assert d.is_empty
    assert "No changes" in d.render()


def test_task_added_and_removed():
    base = _plan([_task("a"), _task("gone")])
    revised = _plan([_task("a"), _task("new")])
    d = diff_plans(base, revised)
    changes = {(e.key, e.change) for e in d.of_kind("task")}
    assert ("gone", "removed") in changes
    assert ("new", "added") in changes


def test_estimate_and_owner_edits_are_field_changes():
    base = _plan([_task("a", owner="tm-1", likely=5)])
    revised = _plan([_task("a", owner="tm-2", likely=8)])
    (edit,) = diff_plans(base, revised).of_kind("task")
    assert edit.change == "modified" and edit.key == "a"
    fields = {fc.field: (fc.before, fc.after) for fc in edit.fields}
    assert fields["owner_id"] == ("tm-1", "tm-2")
    assert fields["likely"] == (5, 8)


def test_rejected_and_added_dependencies():
    base = _plan([_task("a"), _task("b"), _task("c")], [_dep("a", "b")])
    revised = _plan([_task("a"), _task("b"), _task("c")], [_dep("b", "c")])
    deps = {(e.key, e.change) for e in diff_plans(base, revised).of_kind("dependency")}
    assert ("a -> b", "removed") in deps  # human rejected the agent's edge
    assert ("b -> c", "added") in deps  # human added one


def test_dependency_lag_change_is_a_field_change():
    base = _plan([_task("a"), _task("b")], [_dep("a", "b", lag=0)])
    revised = _plan([_task("a"), _task("b")], [_dep("a", "b", lag=3)])
    (edit,) = diff_plans(base, revised).of_kind("dependency")
    assert edit.change == "modified"
    assert edit.fields[0].field == "lag" and edit.fields[0].after == 3
