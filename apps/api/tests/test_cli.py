"""Tests for the `plan` CLI's pure pipeline (no LLM call).

Exercises fixture loading, plan assembly, deterministic validation, and the
golden comparison against the real flagship fixture — the live agent step
(`cmd_breakdown` → `WorkBreakdownAgent.run`) needs credentials and is not run here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.cli import (
    assemble_plan,
    compare_to_golden,
    decisions_sidecar,
    load_decision_record,
    load_fixture,
    resolve_prd,
)
from planner_core import (
    Confidence,
    DecisionRecord,
    Epic,
    FlaggedIssue,
    Plan,
    Provenance,
    Task,
    ThreePointEstimate,
    WorkBreakdown,
    build_report,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "jira-cloud-migration"


def _prov(quote: str) -> Provenance:
    return Provenance(
        reasoning="r",
        source_quote=quote,
        source_section="Background",
        confidence=Confidence.HIGH,
        agent="work-breakdown",
        model="claude-test",
        timestamp=datetime(2026, 7, 23, tzinfo=UTC),
    )


def test_load_fixture_reads_all_three_inputs():
    fx = load_fixture(FIXTURE)
    assert "Jira Cloud" in fx.prd_text
    assert any(m.id == "tm-tpm" for m in fx.team)
    assert any(c.id == "con-license" for c in fx.constraints)


def test_assemble_plan_folds_wbs_with_inputs_and_validates():
    fx = load_fixture(FIXTURE)
    # A tiny breakdown whose owner + quote are real, so it validates clean.
    quote = "our current Data Center license expires 2027-04-30, and that date is hard"
    wb = WorkBreakdown(
        epics=[Epic(id="epic-x", name="Assessment", provenance=_prov(quote))],
        tasks=[
            Task(
                id="task-x",
                name="Inventory projects",
                epic_id="epic-x",
                owner_id="tm-jira-admin",
                estimate=ThreePointEstimate(optimistic=2, likely=3, pessimistic=6),
                provenance=_prov(quote),
            )
        ],
    )

    plan = assemble_plan(
        plan_id="plan-test",
        name="test",
        source_document="fixtures/jira-cloud-migration/prd.md",
        breakdown=wb,
        team=fx.team,
        constraints=fx.constraints,
    )

    # Inputs are folded in; dependencies/milestones stay empty for now.
    assert plan.team == fx.team
    assert plan.constraints == fx.constraints
    assert plan.dependencies == [] and plan.milestones == []

    report = build_report(plan, fx.prd_text)
    assert report.ok  # real owner, verbatim quote → no errors


def test_assemble_plan_produces_a_loadable_plan():
    wb = WorkBreakdown()
    plan = assemble_plan(
        plan_id="p",
        name="empty",
        source_document="x",
        breakdown=wb,
        team=[],
        constraints=[],
    )
    # Round-trips through the P1.2 model.
    assert Plan.model_validate_json(plan.model_dump_json()) == plan


def test_compare_to_golden_matches_on_task_names():
    golden = Plan.model_validate_json((FIXTURE / "golden" / "expected-plan.json").read_text())
    # Reuse the golden's own tasks as the "produced" plan → full name match.
    produced = Plan(id="p", name="p", tasks=golden.tasks, epics=golden.epics)
    summary = compare_to_golden(produced, golden)
    assert f"name-matched {len(golden.tasks)}/{len(golden.tasks)}" in summary


def test_resolve_prd_prefers_explicit_then_source_document(tmp_path):
    prd = FIXTURE / "prd.md"
    # source_document points at the real PRD (relative to repo root / cwd).
    plan = Plan(id="p", name="p", source_document=str(prd))
    assert resolve_prd(plan, tmp_path / "plan.json", explicit=None) is not None

    # Explicit --prd wins.
    explicit = tmp_path / "other.md"
    explicit.write_text("EXPLICIT PRD")
    assert resolve_prd(plan, tmp_path / "plan.json", explicit=str(explicit)) == "EXPLICIT PRD"


def test_resolve_prd_returns_none_when_unlocatable(tmp_path):
    plan = Plan(id="p", name="p", source_document="/nowhere/prd.md")
    assert resolve_prd(plan, tmp_path / "plan.json", explicit=None) is None


def test_decisions_sidecar_path_sits_beside_the_plan(tmp_path):
    assert decisions_sidecar(tmp_path / "plan.json") == tmp_path / "plan.decisions.json"


def test_load_decision_record_round_trips_the_sidecar(tmp_path):
    plan_path = tmp_path / "plan.json"
    record = DecisionRecord(
        flagged=[
            FlaggedIssue(severity="warning", code="low-confidence", message="m", entity_id="a")
        ]
    )
    decisions_sidecar(plan_path).write_text(record.model_dump_json())
    assert load_decision_record(plan_path) == record


def test_load_decision_record_absent_is_none(tmp_path):
    assert load_decision_record(tmp_path / "plan.json") is None


def test_build_scenario_parses_slip_and_dep_flags():
    from argparse import Namespace

    from app.cli import _build_scenario
    from planner_core import AddDependency, DelayTask, RemoveDependency

    scenario = _build_scenario(
        Namespace(
            name="what-if",
            slip=["task-a:5", "task-b:2.5"],
            add_dep=["task-a:task-c"],
            remove_dep=["task-x:task-y"],
        )
    )
    assert scenario.name == "what-if"
    kinds = [type(c) for c in scenario.changes]
    assert kinds == [DelayTask, DelayTask, AddDependency, RemoveDependency]
    assert scenario.changes[0].task_id == "task-a" and scenario.changes[0].days == 5.0
    assert scenario.changes[2].predecessor_id == "task-a"
    assert scenario.changes[3].successor_id == "task-y"
