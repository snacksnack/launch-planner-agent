"""Tests for plan-vs-baseline comparison (RC1-192)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from planner_core import (
    Confidence,
    Dependency,
    Plan,
    Provenance,
    Task,
    TeamMember,
    ThreePointEstimate,
    compare_versions,
)

MONDAY = date(2026, 8, 3)
NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _prov() -> Provenance:
    return Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=NOW,
    )


def _task(tid: str, likely: float) -> Task:
    return Task(
        id=tid, name=tid, owner_id="tm-1",
        estimate=ThreePointEstimate(optimistic=likely, likely=likely, pessimistic=likely),
        provenance=_prov(),
    )


def _dep(pred: str, succ: str) -> Dependency:
    return Dependency(
        id=f"d-{pred}-{succ}", predecessor_id=pred, successor_id=succ, provenance=_prov()
    )


def _plan(tasks, deps=None) -> Plan:
    return Plan(
        id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")],
        tasks=tasks, dependencies=deps or [],
    )


# A -> B, both on the critical path.
def _baseline() -> Plan:
    return _plan([_task("A", 5), _task("B", 3)], [_dep("A", "B")])


def test_identical_plan_is_on_track():
    comparison = compare_versions(_baseline(), _baseline(), start_date=MONDAY)
    assert comparison.is_on_track
    assert comparison.plan_diff.is_empty
    assert comparison.schedule_delta.finish_shift_days == 0


def test_estimate_edit_shows_variance_in_structure_and_schedule():
    current = _plan([_task("A", 5), _task("B", 8)], [_dep("A", "B")])  # B grew 3 -> 8
    comparison = compare_versions(_baseline(), current, start_date=MONDAY)

    assert not comparison.is_on_track
    # Structural: B's estimate changed.
    (change,) = [e for e in comparison.plan_diff.entities if e.key == "B"]
    assert change.change == "modified"
    assert any(f.field == "likely" for f in change.fields)
    # Schedule: the launch slipped by the extra 5 days on the critical path.
    assert comparison.schedule_delta.finish_shift_days == 5
    assert "slips 5 working day(s)" in comparison.schedule_delta.headline


def test_added_task_shows_in_structural_diff():
    current = _plan([_task("A", 5), _task("B", 3), _task("C", 2)], [_dep("A", "B")])
    comparison = compare_versions(_baseline(), current, start_date=MONDAY)
    added = [e for e in comparison.plan_diff.entities if e.change == "added"]
    assert any(e.key == "C" for e in added)


def test_render_labels_the_source_as_the_baseline():
    current = _plan([_task("A", 5), _task("B", 8)], [_dep("A", "B")])
    text = compare_versions(_baseline(), current, start_date=MONDAY).render()
    assert "the baseline" in text  # not "the agent proposal"
    assert "No structural changes" not in text
