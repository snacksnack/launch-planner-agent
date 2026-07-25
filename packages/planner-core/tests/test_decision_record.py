"""Tests for the durable decision record (RC1-197)."""

from __future__ import annotations

from datetime import UTC, datetime

from planner_core import (
    Confidence,
    CycleBreak,
    DecisionRecord,
    EdgeRejection,
    Plan,
    Provenance,
    Task,
    TeamMember,
    ThreePointEstimate,
    build_decision_record,
)

NOW = datetime(2026, 7, 25, tzinfo=UTC)
PRD = "The team must inventory all projects before planning the waves."


def _prov(quote: str, *, confidence: Confidence = Confidence.HIGH) -> Provenance:
    return Provenance(
        reasoning="r", source_quote=quote, source_section=None, confidence=confidence,
        agent="a", model="m", timestamp=NOW,
    )


def _task(tid: str, quote: str, *, confidence: Confidence = Confidence.HIGH) -> Task:
    return Task(
        id=tid, name=tid, owner_id="tm-1",
        estimate=ThreePointEstimate(optimistic=1, likely=2, pessimistic=3),
        provenance=_prov(quote, confidence=confidence),
    )


def _plan(*tasks: Task) -> Plan:
    return Plan(id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")], tasks=list(tasks))


def test_captures_rejected_and_cycle_broken_edges():
    plan = _plan(_task("a", "inventory all projects"))
    rejected = [
        EdgeRejection(0, "a", "ghost", "dangling-reference", "references unknown id 'ghost'")
    ]
    cycle_breaks = [
        CycleBreak("dep-2", "b", "a", ("a", "b"), "removed lowest-confidence edge")
    ]
    record = build_decision_record(plan, PRD, rejected=rejected, cycle_breaks=cycle_breaks)

    assert len(record.rejected_edges) == 1
    assert record.rejected_edges[0].successor_id == "ghost"
    assert record.rejected_edges[0].code == "dangling-reference"
    assert len(record.cycle_breaks) == 1
    assert record.cycle_breaks[0].cycle == ["a", "b"]


def test_flags_low_confidence_entities():
    plan = _plan(
        _task("a", "inventory all projects"),
        _task("b", "plan the waves", confidence=Confidence.LOW),
    )
    record = build_decision_record(plan, PRD)
    low = [f for f in record.flagged if f.code == "low-confidence"]
    assert [f.entity_id for f in low] == ["b"]


def test_without_prd_drops_source_dependent_checks():
    # A quote that isn't in the (empty) PRD would be flagged unverifiable with a
    # source; with no PRD we suppress that check rather than emit false positives.
    plan = _plan(_task("a", "a quote that is not in any prd"))
    with_prd = build_decision_record(plan, "unrelated text")
    without_prd = build_decision_record(plan, None)

    assert any(f.code == "unverifiable-quote" for f in with_prd.flagged)
    assert not any(f.code == "unverifiable-quote" for f in without_prd.flagged)
    assert without_prd.coverage_gaps == []


def test_round_trips_through_json():
    plan = _plan(_task("a", "inventory all projects", confidence=Confidence.LOW))
    record = build_decision_record(plan, PRD)
    assert DecisionRecord.model_validate_json(record.model_dump_json()) == record


def test_is_empty_reports_a_clean_record():
    assert DecisionRecord().is_empty is True
    # A lone task legitimately flags orphan-task, so the record isn't empty.
    record = build_decision_record(_plan(_task("a", "inventory all projects")), PRD)
    assert record.is_empty is False
