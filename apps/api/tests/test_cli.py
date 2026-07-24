"""Tests for the `plan` CLI's pure pipeline (no LLM call).

Exercises fixture loading, plan assembly, deterministic validation, and the
golden comparison against the real flagship fixture — the live agent step
(`cmd_breakdown` → `WorkBreakdownAgent.run`) needs credentials and is not run here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.cli import assemble_plan, compare_to_golden, load_fixture
from planner_core import (
    Confidence,
    Epic,
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
