"""Tests for deterministic dependency-graph validation (networkx, no LLM)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from planner_core import (
    Confidence,
    Constraint,
    ConstraintType,
    Dependency,
    Plan,
    Provenance,
    Task,
    TeamMember,
    ThreePointEstimate,
    build_dependency_report,
    filter_dependencies,
    find_cycles,
    orphan_tasks,
    resolve_cycles,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "jira-cloud-migration"


def _prov(quote: str = "n/a", *, confidence: Confidence = Confidence.HIGH) -> Provenance:
    return Provenance(
        reasoning="r",
        source_quote=quote,
        source_section=None,
        confidence=confidence,
        agent="dependency",
        model="test",
        timestamp=datetime(2026, 7, 24, tzinfo=UTC),
    )


def _task(tid: str) -> Task:
    return Task(
        id=tid,
        name=tid,
        estimate=ThreePointEstimate(optimistic=1, likely=1, pessimistic=1),
        provenance=_prov(),
    )


def _dep(did: str, pred: str, succ: str, **prov_kwargs) -> Dependency:
    return Dependency(
        id=did, predecessor_id=pred, successor_id=succ, provenance=_prov(**prov_kwargs)
    )


def _edge(pred: str, succ: str) -> SimpleNamespace:
    return SimpleNamespace(predecessor_id=pred, successor_id=succ)


def _plan(tasks, deps=None, constraints=None) -> Plan:
    return Plan(
        id="p",
        name="p",
        team=[TeamMember(id="tm-1", name="Ada")],
        tasks=tasks,
        dependencies=deps or [],
        constraints=constraints or [],
    )


# --- filter_dependencies ---------------------------------------------------


def test_filter_drops_dangling_self_loop_and_duplicate_edges():
    task_ids = {"a", "b"}
    edges = [
        _edge("a", "b"),  # ok
        _edge("a", "ghost"),  # dangling
        _edge("a", "a"),  # self-loop
        _edge("a", "b"),  # duplicate
    ]
    accepted, rejected = filter_dependencies(edges, task_ids)
    assert len(accepted) == 1
    codes = sorted(r.code for r in rejected)
    assert codes == ["dangling-reference", "duplicate-edge", "self-loop"]


def test_filter_accepts_milestone_ids_as_endpoints():
    # A task -> milestone edge is valid when the milestone id is a known endpoint;
    # it's only dangling if the id isn't in the endpoint set (RC1-198).
    endpoint_ids = {"task-a", "ms-1"}
    edges = [_edge("task-a", "ms-1"), _edge("task-a", "ms-ghost")]
    accepted, rejected = filter_dependencies(edges, endpoint_ids)
    assert [(e.predecessor_id, e.successor_id) for e in accepted] == [("task-a", "ms-1")]
    assert [r.code for r in rejected] == ["dangling-reference"]


# --- cycle detection (AC1) -------------------------------------------------


def test_induced_cycle_is_caught_and_reported():
    tasks = [_task("a"), _task("b"), _task("c")]
    deps = [_dep("d1", "a", "b"), _dep("d2", "b", "c"), _dep("d3", "c", "a")]
    plan = _plan(tasks, deps)

    cycles = find_cycles(plan)
    assert cycles and set(cycles[0]) == {"a", "b", "c"}

    report = build_dependency_report(plan, "")
    assert not report.ok
    cycle_errors = [i for i in report.errors if i.code == "dependency-cycle"]
    assert len(cycle_errors) == 1
    assert "->" in cycle_errors[0].message


def test_acyclic_plan_has_no_cycle_errors():
    tasks = [_task("a"), _task("b")]
    plan = _plan(tasks, [_dep("d1", "a", "b")])
    assert find_cycles(plan) == []
    assert build_dependency_report(plan, "").ok


# --- cycle resolution ------------------------------------------------------


def test_resolve_cycles_drops_the_lowest_confidence_edge():
    deps = [
        _dep("d1", "a", "b"),  # high
        _dep("d2", "b", "c"),  # high
        _dep("d3", "c", "a", confidence=Confidence.LOW),  # weakest -> victim
    ]
    kept, breaks = resolve_cycles(deps)
    assert {d.id for d in kept} == {"d1", "d2"}
    assert len(breaks) == 1
    assert breaks[0].removed_edge_id == "d3"
    assert breaks[0].predecessor_id == "c" and breaks[0].successor_id == "a"


def test_resolve_cycles_is_deterministic_on_ties():
    # All equal confidence -> tie broken by edge id (smallest wins the victim slot).
    deps = [_dep("d1", "a", "b"), _dep("d2", "b", "c"), _dep("d3", "c", "a")]
    kept, breaks = resolve_cycles(deps)
    assert len(breaks) == 1
    assert breaks[0].removed_edge_id == "d1"
    # Result is acyclic.
    plan = _plan([_task("a"), _task("b"), _task("c")], kept)
    assert find_cycles(plan) == []


def test_resolve_cycles_leaves_acyclic_graphs_untouched():
    deps = [_dep("d1", "a", "b"), _dep("d2", "b", "c")]
    kept, breaks = resolve_cycles(deps)
    assert [d.id for d in kept] == ["d1", "d2"]
    assert breaks == []


def test_report_surfaces_cycle_breaks_as_warnings():
    tasks = [_task("a"), _task("b")]
    kept = [_dep("d1", "a", "b")]  # already acyclic after a hypothetical break
    _, breaks = resolve_cycles([_dep("d1", "a", "b"), _dep("d2", "b", "a")])
    report = build_dependency_report(_plan(tasks, kept), "", cycle_breaks=breaks)
    assert report.ok  # plan is schedulable again
    assert "cycle-break" in report.render()


# --- orphans & gate coverage ----------------------------------------------


def test_orphan_task_is_flagged_as_warning():
    tasks = [_task("a"), _task("b"), _task("lonely")]
    plan = _plan(tasks, [_dep("d1", "a", "b")])
    assert orphan_tasks(plan) == ["lonely"]
    report = build_dependency_report(plan, "")
    assert report.ok  # orphans are warnings, not errors
    assert any(i.code == "orphan-task" for i in report.warnings)


def test_unenforced_gate_is_flagged():
    tasks = [_task("gate-task"), _task("gated-task")]
    gate = Constraint(
        id="con-gate",
        type=ConstraintType.GATE,
        description="x",
        gate="review before work",
        applies_to=["gated-task"],
        provenance=_prov(),
    )
    # No edge enforces the gate -> warning.
    plan = _plan(tasks, deps=[], constraints=[gate])
    report = build_dependency_report(plan, "")
    assert any(i.code == "unenforced-gate" for i in report.warnings)

    # Add the enforcing edge -> no warning.
    plan2 = _plan(tasks, deps=[_dep("d1", "gate-task", "gated-task")], constraints=[gate])
    report2 = build_dependency_report(plan2, "")
    assert not any(i.code == "unenforced-gate" for i in report2.warnings)


# --- provenance guards -----------------------------------------------------


def test_unverifiable_dependency_quote_flagged():
    tasks = [_task("a"), _task("b")]
    dep = _dep("d1", "a", "b", quote="a quote that is not in the prd")
    report = build_dependency_report(_plan(tasks, [dep]), "the prd says other things")
    assert any(i.code == "unverifiable-quote" for i in report.warnings)


# --- real golden data ------------------------------------------------------


def test_flagship_golden_dependency_graph_is_clean():
    """The hand-authored golden (28 task edges + 4 milestone links) validates clean."""
    plan = Plan.model_validate_json((FIXTURE / "golden" / "expected-plan.json").read_text())
    prd = (FIXTURE / "prd.md").read_text()
    report = build_dependency_report(plan, prd)
    assert report.ok, report.render()
    assert report.dependency_count == 32
    # The last 4 edges link each milestone to the task that completes it (RC1-198).
    milestone_edges = [
        d for d in plan.dependencies if d.successor_id.startswith("ms-")
    ]
    assert {d.successor_id for d in milestone_edges} == {
        "ms-pilot", "ms-bulk", "ms-golive", "ms-decom",
    }
    # Every dependency quote is verbatim in the PRD.
    assert not any(i.code == "unverifiable-quote" for i in report.warnings)
