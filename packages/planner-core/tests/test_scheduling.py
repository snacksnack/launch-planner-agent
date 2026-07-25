"""Exhaustive CPM tests: hand-computed textbook examples + calendar + real data."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from planner_core import (
    Confidence,
    Constraint,
    ConstraintType,
    Dependency,
    DependencyType,
    Milestone,
    Plan,
    Provenance,
    Task,
    ThreePointEstimate,
    WorkingCalendar,
    compute_cpm,
    critical_paths,
    schedule_plan,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "jira-cloud-migration"
FS = DependencyType.FINISH_TO_START


def _fs(a: str, b: str, lag: float = 0.0):
    return (a, b, FS, lag)


# --- pure CPM: the textbook example ----------------------------------------
#
#   A(3) -> B(4) -> D(5) -> F(2)
#   A(3) -> C(2) -> E(1) -> F(2)
#
# Hand-computed: critical path A-B-D-F, project duration 14.


def test_textbook_cpm_early_late_float_and_critical_path():
    durations = {"A": 3, "B": 4, "C": 2, "D": 5, "E": 1, "F": 2}
    edges = [
        _fs("A", "B"), _fs("A", "C"), _fs("B", "D"),
        _fs("C", "E"), _fs("D", "F"), _fs("E", "F"),
    ]
    r = compute_cpm(durations, edges)

    assert r.project_duration == 14
    expected = {
        # id: (ES, EF, LS, LF, total_float, free_float, critical)
        "A": (0, 3, 0, 3, 0, 0, True),
        "B": (3, 7, 3, 7, 0, 0, True),
        "C": (3, 5, 9, 11, 6, 0, False),
        "D": (7, 12, 7, 12, 0, 0, True),
        "E": (5, 6, 11, 12, 6, 6, False),
        "F": (12, 14, 12, 14, 0, 0, True),
    }
    for tid, (es, ef, ls, lf, tf, ff, crit) in expected.items():
        n = r.nodes[tid]
        assert (n.early_start, n.early_finish) == (es, ef), tid
        assert (n.late_start, n.late_finish) == (ls, lf), tid
        assert n.total_float == tf and n.free_float == ff, tid
        assert n.is_critical is crit, tid

    assert critical_paths(r) == [["A", "B", "D", "F"]]


def test_start_to_start_relationship():
    # A(5) --SS--> B(3): B can start as soon as A starts.
    r = compute_cpm({"A": 5, "B": 3}, [("A", "B", DependencyType.START_TO_START, 0.0)])
    assert r.project_duration == 5
    assert r.nodes["A"].early_start == 0 and r.nodes["B"].early_start == 0
    assert r.nodes["A"].is_critical is True
    assert r.nodes["B"].total_float == 2  # finishes at 3, project ends at 5


def test_finish_to_start_lag_pushes_successor():
    r = compute_cpm({"A": 3, "B": 2}, [_fs("A", "B", lag=2)])
    assert r.nodes["B"].early_start == 5  # 3 + 2 lag
    assert r.project_duration == 7


def test_multiple_critical_paths_are_all_found():
    # Two equal-length parallel chains between A and E.
    durations = {"A": 1, "B": 2, "C": 2, "E": 1}
    edges = [_fs("A", "B"), _fs("A", "C"), _fs("B", "E"), _fs("C", "E")]
    r = compute_cpm(durations, edges)
    assert all(r.nodes[t].is_critical for t in durations)
    paths = critical_paths(r)
    assert sorted(paths) == [["A", "B", "E"], ["A", "C", "E"]]


def test_cyclic_graph_is_rejected():
    with pytest.raises(ValueError, match="cyclic"):
        compute_cpm({"A": 1, "B": 1}, [_fs("A", "B"), _fs("B", "A")])


# --- working calendar ------------------------------------------------------

MONDAY = date(2026, 8, 3)  # 2026-08-03 is a Monday


def test_calendar_skips_weekends():
    cal = WorkingCalendar(start_date=MONDAY)
    assert cal.nth_working_day(0) == date(2026, 8, 3)  # Mon
    assert cal.nth_working_day(4) == date(2026, 8, 7)  # Fri
    assert cal.nth_working_day(5) == date(2026, 8, 10)  # next Mon, weekend skipped


def test_calendar_skips_blackout_windows():
    cal = WorkingCalendar(
        start_date=MONDAY, blackouts=((date(2026, 8, 5), date(2026, 8, 6)),)  # Wed-Thu freeze
    )
    # index 2 would be Wed, but Wed+Thu are frozen -> Friday.
    assert cal.nth_working_day(2) == date(2026, 8, 7)
    assert cal.is_working_day(date(2026, 8, 5)) is False


def test_signed_working_days_counts_both_directions():
    cal = WorkingCalendar(start_date=MONDAY)
    assert cal.signed_working_days(date(2026, 8, 3), date(2026, 8, 7)) == 4
    assert cal.signed_working_days(date(2026, 8, 7), date(2026, 8, 3)) == -4
    assert cal.signed_working_days(MONDAY, MONDAY) == 0


# --- schedule_plan helpers -------------------------------------------------


def _prov() -> Provenance:
    return Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=date(2026, 8, 3).isoformat() + "T00:00:00Z",
    )


def _task(tid: str, likely: float) -> Task:
    return Task(
        id=tid, name=tid,
        estimate=ThreePointEstimate(optimistic=likely, likely=likely, pessimistic=likely),
        provenance=_prov(),
    )


def _dep(pred: str, succ: str) -> Dependency:
    return Dependency(
        id=f"{pred}-{succ}", predecessor_id=pred, successor_id=succ, provenance=_prov()
    )


def test_schedule_plan_maps_offsets_to_calendar_dates():
    plan = Plan(
        id="p", name="p", tasks=[_task("A", 3), _task("B", 2)], dependencies=[_dep("A", "B")]
    )
    sched = schedule_plan(plan, start_date=MONDAY)

    assert sched.tasks["A"].early_start_date == date(2026, 8, 3)  # Mon
    assert sched.tasks["A"].early_finish_date == date(2026, 8, 5)  # Wed (3 working days)
    assert sched.tasks["B"].early_start_date == date(2026, 8, 6)  # Thu
    assert sched.project_finish_date == date(2026, 8, 7)  # Fri
    assert sched.critical_path_ids == ["A", "B"]


def test_linked_milestone_gets_projected_date_and_slack():
    ms = Milestone(id="ms-1", name="Go-live", target_date=date(2026, 8, 20), provenance=_prov())
    plan = Plan(
        id="p", name="p", tasks=[_task("A", 2)], milestones=[ms],
        dependencies=[_dep("A", "ms-1")],
    )
    sched = schedule_plan(plan, start_date=MONDAY)
    (m,) = sched.milestones
    assert m.scheduled is True
    assert m.projected_date == date(2026, 8, 4)  # after A's 2 working days (Mon, Tue)
    assert m.slack_working_days is not None and m.slack_working_days > 0


def test_unlinked_milestone_is_reported_but_not_scheduled():
    ms = Milestone(id="ms-1", name="Go-live", target_date=date(2026, 8, 20), provenance=_prov())
    plan = Plan(id="p", name="p", tasks=[_task("A", 2)], milestones=[ms])
    sched = schedule_plan(plan, start_date=MONDAY)
    (m,) = sched.milestones
    assert m.scheduled is False
    assert m.projected_date is None and m.slack_working_days is None


def test_hard_date_deadline_negative_float_when_plan_misses():
    task = _task("A", 5)  # finishes Fri 2026-08-07
    late = Constraint(
        id="con-x", type=ConstraintType.HARD_DATE, description="must land by",
        hard_date=date(2026, 8, 5), applies_to=["A"], provenance=_prov(),
    )
    plan = Plan(id="p", name="p", tasks=[task], constraints=[late])
    sched = schedule_plan(plan, start_date=MONDAY)
    (check,) = sched.deadline_checks
    assert check.met is False
    assert check.slack_working_days < 0
    assert sched.meets_all_deadlines is False


def test_hard_date_deadline_met_when_plan_finishes_early():
    task = _task("A", 2)  # finishes Tue 2026-08-04
    ok = Constraint(
        id="con-x", type=ConstraintType.HARD_DATE, description="by",
        hard_date=date(2026, 8, 14), applies_to=["A"], provenance=_prov(),
    )
    plan = Plan(id="p", name="p", tasks=[task], constraints=[ok])
    sched = schedule_plan(plan, start_date=MONDAY)
    assert sched.deadline_checks[0].met is True
    assert sched.meets_all_deadlines is True


# --- real golden data ------------------------------------------------------


def test_schedule_the_flagship_golden_plan():
    plan = Plan.model_validate_json((FIXTURE / "golden" / "expected-plan.json").read_text())
    sched = schedule_plan(plan, start_date=MONDAY)

    assert sched.project_finish_date is not None
    assert sched.project_duration > 0
    assert len(sched.critical_path_ids) > 0
    # con-license (hard_date) targets task-decom-onprem -> a deadline check exists.
    assert any(c.task_id == "task-decom-onprem" for c in sched.deadline_checks)

    # Every golden milestone is wired into the dependency graph (RC1-198), so the
    # scheduler projects a date and slack-to-target for each.
    assert {m.milestone_id for m in sched.milestones} == {
        "ms-pilot", "ms-bulk", "ms-golive", "ms-decom",
    }
    for m in sched.milestones:
        assert m.scheduled, f"{m.milestone_id} should be scheduled"
        assert m.projected_date is not None
        assert m.target_date is not None
        assert m.slack_working_days is not None
