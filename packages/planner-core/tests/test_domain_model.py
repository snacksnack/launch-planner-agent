"""Domain-model tests for the plan schema and its provenance guarantee.

Covers the two RC1-183 acceptance criteria directly:
1. round-trip serialize/deserialize of a fully-populated Plan, and
2. a Plan cannot be constructed with an agent-generated entity missing provenance.
Plus the deterministic validators (estimate ordering, self-dependency,
constraint payload) that keep a malformed plan from ever being built.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from planner_core import (
    Confidence,
    Constraint,
    ConstraintType,
    Dependency,
    DependencyType,
    Epic,
    Milestone,
    Plan,
    Provenance,
    Task,
    TeamMember,
    ThreePointEstimate,
    plan_json_schema,
)
from pydantic import ValidationError


def _prov(**overrides) -> Provenance:
    base = dict(
        reasoning="The PRD lists this explicitly as a deliverable.",
        source_quote="Migrate all projects to Jira Cloud by Q4.",
        source_section="Goals",
        confidence=Confidence.HIGH,
        agent="work-breakdown",
        model="claude-sonnet-5",
        timestamp=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return Provenance(**base)


def _full_plan() -> Plan:
    return Plan(
        id="plan-1",
        name="Jira Cloud Migration",
        description="PRD-derived launch plan.",
        created_at=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        source_document="fixtures/jira-cloud-migration.md",
        team=[TeamMember(id="tm-1", name="Ada Lovelace", role="Backend Engineer")],
        epics=[Epic(id="epic-1", name="Data migration", provenance=_prov())],
        tasks=[
            Task(
                id="task-1",
                name="Export legacy issues",
                epic_id="epic-1",
                owner_id="tm-1",
                estimate=ThreePointEstimate(optimistic=2, likely=3, pessimistic=6),
                provenance=_prov(),
            ),
            Task(
                id="task-2",
                name="Import into Cloud",
                epic_id="epic-1",
                estimate=ThreePointEstimate(optimistic=1, likely=2, pessimistic=5),
                provenance=_prov(confidence=Confidence.MEDIUM),
            ),
        ],
        dependencies=[
            Dependency(
                id="dep-1",
                predecessor_id="task-1",
                successor_id="task-2",
                type=DependencyType.FINISH_TO_START,
                lag=1.0,
                provenance=_prov(),
            )
        ],
        milestones=[
            Milestone(
                id="ms-1",
                name="Cutover complete",
                target_date=date(2026, 12, 15),
                provenance=_prov(confidence=Confidence.LOW),
            )
        ],
        constraints=[
            Constraint(
                id="con-1",
                type=ConstraintType.GATE,
                description="SRE review before production cutover.",
                gate="SRE review",
                applies_to=["ms-1"],
                provenance=_prov(),
            ),
            Constraint(
                id="con-2",
                type=ConstraintType.HARD_DATE,
                description="Legacy contract ends.",
                hard_date=date(2026, 12, 31),
                applies_to=["task-2"],
                provenance=_prov(),
            ),
        ],
    )


# --- AC1: round-trip -------------------------------------------------------


def test_plan_round_trips_through_json():
    plan = _full_plan()
    rebuilt = Plan.model_validate_json(plan.model_dump_json())
    assert rebuilt == plan


def test_round_trip_preserves_provenance_verbatim():
    plan = _full_plan()
    rebuilt = Plan.model_validate_json(plan.model_dump_json())
    assert rebuilt.tasks[0].provenance.source_quote == "Migrate all projects to Jira Cloud by Q4."
    assert rebuilt.milestones[0].provenance.confidence is Confidence.LOW


# --- AC2: provenance is mandatory on agent-produced entities ---------------


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Epic(id="e", name="x"),
        lambda: Task(
            id="t", name="x", estimate=ThreePointEstimate(optimistic=1, likely=1, pessimistic=1)
        ),
        lambda: Dependency(id="d", predecessor_id="a", successor_id="b"),
        lambda: Milestone(id="m", name="x"),
        lambda: Constraint(id="c", type=ConstraintType.GATE, description="x", gate="g"),
    ],
)
def test_agent_entities_require_provenance(factory):
    with pytest.raises(ValidationError, match="provenance"):
        factory()


def test_plan_cannot_hold_task_without_provenance():
    """The acceptance criterion, end to end: no provenance -> no entity -> no plan."""
    with pytest.raises(ValidationError, match="provenance"):
        Plan(
            id="p",
            name="x",
            tasks=[
                {  # dict form: would only validate if provenance weren't required
                    "id": "t",
                    "name": "no prov",
                    "estimate": {"optimistic": 1, "likely": 1, "pessimistic": 1},
                }
            ],
        )


def test_team_member_needs_no_provenance():
    # Human roster input is intentionally exempt.
    assert TeamMember(id="tm", name="Grace").role is None


# --- deterministic validators ---------------------------------------------


def test_three_point_estimate_must_be_ordered():
    with pytest.raises(ValidationError, match="optimistic <= likely <= pessimistic"):
        ThreePointEstimate(optimistic=5, likely=2, pessimistic=3)


def test_three_point_pert_expected_and_std_dev():
    est = ThreePointEstimate(optimistic=2, likely=4, pessimistic=12)
    assert est.expected == pytest.approx((2 + 16 + 12) / 6)
    assert est.std_dev == pytest.approx((12 - 2) / 6)


def test_dependency_rejects_self_loop():
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        Dependency(id="d", predecessor_id="t1", successor_id="t1", provenance=_prov())


def test_hard_date_constraint_requires_a_date():
    with pytest.raises(ValidationError, match="requires 'hard_date'"):
        Constraint(id="c", type=ConstraintType.HARD_DATE, description="x", provenance=_prov())


def test_gate_constraint_requires_a_gate():
    with pytest.raises(ValidationError, match="requires 'gate'"):
        Constraint(id="c", type=ConstraintType.GATE, description="x", provenance=_prov())


def test_blackout_constraint_requires_a_valid_window():
    from datetime import date

    with pytest.raises(ValidationError, match="window_start"):
        Constraint(id="c", type=ConstraintType.BLACKOUT, description="freeze", provenance=_prov())
    with pytest.raises(ValidationError, match="window_start <= window_end"):
        Constraint(
            id="c", type=ConstraintType.BLACKOUT, description="freeze",
            window_start=date(2027, 1, 4), window_end=date(2026, 11, 15), provenance=_prov(),
        )

    freeze = Constraint(
        id="c", type=ConstraintType.BLACKOUT, description="freeze",
        window_start=date(2026, 11, 15), window_end=date(2027, 1, 4), provenance=_prov(),
    )
    assert freeze.covers(date(2026, 12, 1)) is True
    assert freeze.covers(date(2026, 10, 1)) is False


def test_extra_fields_are_forbidden():
    with pytest.raises(ValidationError):
        TeamMember(id="tm", name="Grace", unexpected="nope")


# --- published schema ------------------------------------------------------


def test_plan_json_schema_is_publishable_and_embeds_provenance():
    schema = plan_json_schema()
    assert schema["title"] == "Plan"
    # Provenance must be discoverable in the schema agents are forced against.
    assert "Provenance" in schema["$defs"]
    assert set(schema["$defs"]["Provenance"]["required"]) >= {
        "reasoning",
        "source_quote",
        "confidence",
        "agent",
        "model",
        "timestamp",
    }
