"""Tests for the deterministic RAID analysis + validation (RC1-191)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from planner_core import (
    Confidence,
    Constraint,
    ConstraintType,
    Dependency,
    Plan,
    PrdEvidence,
    Provenance,
    RaidItem,
    RaidProvenance,
    RaidType,
    ScheduleEvidence,
    Task,
    TeamMember,
    ThreePointEstimate,
    analyze_schedule_risks,
    build_raid_report,
    schedule_plan,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "jira-cloud-migration"
MONDAY = date(2026, 8, 3)
NOW = datetime(2026, 7, 26, tzinfo=UTC)


def _prov() -> Provenance:
    return Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=NOW,
    )


def _task(tid: str, likely: float, owner: str | None) -> Task:
    return Task(
        id=tid, name=tid, owner_id=owner,
        estimate=ThreePointEstimate(optimistic=likely, likely=likely, pessimistic=likely),
        provenance=_prov(),
    )


def _dep(pred: str, succ: str) -> Dependency:
    return Dependency(
        id=f"d-{pred}-{succ}", predecessor_id=pred, successor_id=succ, provenance=_prov()
    )


def _raid(
    rid: str, rtype: RaidType, evidence, *, owner=None, prob=None, impact=None,
    mitigation=None, rationale=None, confidence=Confidence.HIGH,
) -> RaidItem:
    return RaidItem(
        id=rid, type=rtype, title=rid, description="desc",
        probability=prob, impact=impact, mitigation=mitigation,
        suggested_owner_id=owner, rationale=rationale,
        provenance=RaidProvenance(
            reasoning="r", confidence=confidence, evidence=evidence,
            agent="raid", model="m", timestamp=NOW,
        ),
    )


# --- schedule-fact analysis ------------------------------------------------


def _golden() -> Plan:
    return Plan.model_validate_json((FIXTURE / "golden" / "expected-plan.json").read_text())


def test_analyzer_flags_single_owner_critical_path_on_the_golden():
    """AC: the flagship yields at least one schedule-derived risk fact."""
    plan = _golden()
    facts = analyze_schedule_risks(plan, schedule_plan(plan, start_date=MONDAY))
    single = [f for f in facts if f.code == "single-owner-critical-path"]
    assert single, "expected a single-owner-critical-path fact"
    # The top offender owns several critical tasks and is cited by id.
    top = single[0]
    assert top.severity_hint == "high"
    assert any(e.startswith("task-") for e in top.entity_ids)
    assert any(e.startswith("tm-") for e in top.entity_ids)


def test_analyzer_flags_zero_float_and_missed_deadline():
    # A -> B, both critical; a hard date B can't meet.
    late = Constraint(
        id="con-x", type=ConstraintType.HARD_DATE, description="by",
        hard_date=date(2026, 8, 4), applies_to=["B"], provenance=_prov(),
    )
    plan = Plan(
        id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")],
        tasks=[_task("A", 3, "tm-1"), _task("B", 3, "tm-1")],
        dependencies=[_dep("A", "B")], constraints=[late],
    )
    facts = analyze_schedule_risks(plan, schedule_plan(plan, start_date=MONDAY))
    codes = {f.code for f in facts}
    assert "zero-float-critical-path" in codes
    assert "missed-deadline" in codes
    # Both A and B on one owner -> single-owner fact too.
    assert "single-owner-critical-path" in codes


# --- validation ------------------------------------------------------------


def test_golden_raid_log_validates_clean():
    plan = _golden()
    prd = (FIXTURE / "prd.md").read_text()
    report = build_raid_report(plan, prd)
    assert report.ok, report.render()
    assert report.item_count == 5
    assert not report.warnings  # the hand-authored golden is clean


def test_report_flags_unknown_owner_and_unverifiable_quote():
    plan = Plan(
        id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")],
        raid=[
            _raid(
                "r1", RaidType.RISK,
                PrdEvidence(source_quote="not in the prd at all"),
                owner="ghost", prob=3, impact=3, mitigation="do the thing",
            ),
        ],
    )
    report = build_raid_report(plan, "some unrelated prd text")
    assert not report.ok  # unknown owner is an error
    assert any(i.code == "unknown-owner" for i in report.errors)
    assert any(i.code == "unverifiable-quote" for i in report.warnings)


def test_report_flags_unscored_risk_missing_mitigation_and_decision_rationale():
    plan = Plan(
        id="p", name="p",
        raid=[
            _raid("r1", RaidType.RISK, ScheduleEvidence(fact_code="x", statement="s")),
            _raid("d1", RaidType.DECISION, PrdEvidence(source_quote="q")),
        ],
    )
    report = build_raid_report(plan, "q")  # quote verbatim so no unverifiable warning
    codes = {i.code for i in report.warnings}
    assert {"unscored-risk", "no-mitigation", "no-rationale"} <= codes


def test_report_flags_dangling_schedule_evidence():
    plan = Plan(
        id="p", name="p",
        raid=[
            _raid(
                "r1", RaidType.RISK,
                ScheduleEvidence(fact_code="x", statement="s", entity_ids=["nope"]),
                prob=2, impact=2, mitigation="m",
            )
        ],
    )
    report = build_raid_report(plan, "")
    assert any(i.code == "dangling-evidence" for i in report.warnings)


def test_severity_is_probability_times_impact():
    item = _raid(
        "r1", RaidType.RISK, ScheduleEvidence(fact_code="x", statement="s"),
        prob=3, impact=4, mitigation="m",
    )
    assert item.severity == 12
    unscored = _raid("a1", RaidType.ASSUMPTION, PrdEvidence(source_quote="q"))
    assert unscored.severity is None
