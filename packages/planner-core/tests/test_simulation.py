"""Tests for the slippage simulator (RC1-190).

The two acceptance criteria are the headline cases: slipping a critical-path task
moves the launch by that amount; slipping a task with enough float shows zero
launch impact. Uses small hand-built plans (exact float known) plus the flagship
golden.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from planner_core import (
    AddDependency,
    Confidence,
    DelayTask,
    Dependency,
    Plan,
    Provenance,
    RemoveDependency,
    Scenario,
    SetEstimate,
    Task,
    TeamMember,
    ThreePointEstimate,
    apply_scenario,
    schedule_plan,
    simulate,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "jira-cloud-migration"
MONDAY = date(2026, 8, 3)


def _prov() -> Provenance:
    return Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=datetime(2026, 7, 26, tzinfo=UTC),
    )


def _task(tid: str, likely: float) -> Task:
    return Task(
        id=tid, name=tid, owner_id="tm-1",
        estimate=ThreePointEstimate(optimistic=likely, likely=likely, pessimistic=likely),
        provenance=_prov(),
    )


def _dep(pred: str, succ: str) -> Dependency:
    return Dependency(
        id=f"dep-{pred}-{succ}", predecessor_id=pred, successor_id=succ, provenance=_prov()
    )


def _sim(plan: Plan, *changes):
    return simulate(plan, Scenario(changes=list(changes)), start_date=MONDAY)


def _plan(tasks, deps) -> Plan:
    return Plan(
        id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")], tasks=tasks, dependencies=deps
    )


# A -> C, B -> C. A=5 (critical), B=2 (float 3), C=3. Finish = 8 working days.
def _diamond() -> Plan:
    return _plan(
        [_task("A", 5), _task("B", 2), _task("C", 3)],
        [_dep("A", "C"), _dep("B", "C")],
    )


# --- acceptance criteria ---------------------------------------------------


def test_slipping_a_critical_task_moves_launch_by_that_amount():
    scenario = Scenario(changes=[DelayTask(task_id="A", days=4)])
    result = simulate(_diamond(), scenario, start_date=MONDAY)
    # A is on the critical path (A=5 drives C); +4 pushes the finish out by 4.
    assert result.delta.finish_shift_days == 4
    assert result.delta.has_launch_impact
    assert "slips 4 working day(s)" in result.delta.headline


def test_slipping_a_task_within_its_float_has_zero_launch_impact():
    # B has 3 days of total float; a 2-day slip is fully absorbed.
    scenario = Scenario(changes=[DelayTask(task_id="B", days=2)])
    result = simulate(_diamond(), scenario, start_date=MONDAY)
    assert result.delta.finish_shift_days == 0
    assert not result.delta.has_launch_impact
    assert "absorbed by available float" in result.delta.headline


def test_slip_beyond_float_moves_launch_by_the_overflow():
    # B float is 3; a 5-day slip overflows by 2, so the launch moves 2.
    result = _sim(_diamond(), DelayTask(task_id="B", days=5))
    assert result.delta.finish_shift_days == 2


# --- diff detail -----------------------------------------------------------


def test_delta_reports_task_that_joined_the_critical_path():
    # Slip B past its float: B joins the critical path.
    result = _sim(_diamond(), DelayTask(task_id="B", days=5))
    joined = {n.id for n in result.delta.critical_joined}
    assert "B" in joined
    assert any(s.task_id == "B" and s.became_critical for s in result.delta.task_shifts)


def test_delta_names_entities_for_plain_language():
    result = _sim(_diamond(), DelayTask(task_id="A", days=1))
    shift = next(s for s in result.delta.task_shifts if s.task_id == "C")
    assert shift.task_name == "C"
    assert shift.finish_shift_days == 1


# --- scenario application robustness ---------------------------------------


def test_unknown_ids_and_bad_edges_become_warnings_not_errors():
    scenario = Scenario(changes=[
        DelayTask(task_id="ghost", days=3),
        AddDependency(predecessor_id="C", successor_id="A"),  # would create a cycle A->C->A
        RemoveDependency(predecessor_id="X", successor_id="Y"),  # no such edge
        SetEstimate(task_id="A", optimistic=9, likely=1),  # violates ordering
    ])
    applied, warnings = apply_scenario(_diamond(), scenario)
    assert len(warnings) == 4
    assert any("unknown task 'ghost'" in w for w in warnings)
    assert any("would create a cycle" in w for w in warnings)
    assert any("no edge X -> Y" in w for w in warnings)
    assert any("violate" in w for w in warnings)
    # Nothing was actually applied, so the schedule is unchanged.
    assert applied.dependencies == _diamond().dependencies


def test_removing_a_dependency_can_pull_the_launch_in():
    # Remove A->C: C no longer waits on A(5), only B(2). Finish 8 -> 5.
    result = simulate(
        _diamond(), Scenario(changes=[RemoveDependency(predecessor_id="A", successor_id="C")]),
        start_date=MONDAY,
    )
    assert result.delta.finish_shift_days == -3
    assert "pulls in 3 working day(s)" in result.delta.headline


def test_added_dependency_is_stamped_and_reschedules():
    # Add B->A: A now waits on B(2), so A starts at 2, finishes 7, C finishes 10. +2.
    result = simulate(
        _diamond(), Scenario(changes=[AddDependency(predecessor_id="B", successor_id="A")]),
        start_date=MONDAY,
    )
    assert result.delta.finish_shift_days == 2
    added = [d for d in result.simulated_plan.dependencies if d.id.startswith("sim-dep-")]
    assert len(added) == 1 and added[0].provenance.agent == "simulation"


# --- golden data + serialization -------------------------------------------


def test_simulate_on_the_flagship_golden_and_deadline_flip():
    plan = Plan.model_validate_json((FIXTURE / "golden" / "expected-plan.json").read_text())
    # The license deadline has ~147 working days of slack; a slip past that misses it.
    scenario = Scenario(
        name="decom slips two quarters", changes=[DelayTask(task_id="task-decom-onprem", days=200)]
    )
    result = simulate(plan, scenario, start_date=MONDAY)
    assert result.delta.finish_shift_days > 0
    # The hard-date license deadline check should flip met -> missed.
    flipped = [f for f in result.delta.deadline_flips if not f.met_after and f.met_before]
    assert flipped, "a 200-day slip on decommissioning should miss the license deadline"


def test_delta_round_trips_through_json():
    result = _sim(_diamond(), DelayTask(task_id="A", days=4))
    from planner_core import ScheduleDelta

    assert ScheduleDelta.model_validate_json(result.delta.model_dump_json()) == result.delta


def test_baseline_matches_a_plain_schedule():
    plan = _diamond()
    result = simulate(plan, Scenario(changes=[]), start_date=MONDAY)
    plain = schedule_plan(plan, start_date=MONDAY)
    assert result.baseline.project_finish_date == plain.project_finish_date
    assert result.delta.finish_shift_days == 0
