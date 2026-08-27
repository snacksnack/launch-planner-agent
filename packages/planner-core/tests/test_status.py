"""Tests for weekly status assembly + the rule-based health signal (RC1-194)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from planner_core import (
    Confidence,
    Constraint,
    ConstraintType,
    Dependency,
    Health,
    Plan,
    PrdEvidence,
    Provenance,
    RaidItem,
    RaidProvenance,
    RaidType,
    Task,
    TeamMember,
    ThreePointEstimate,
    assemble_status,
    compare_versions,
    fallback_narrative,
    render_html,
    render_markdown,
)

MONDAY = date(2026, 8, 3)
NOW = datetime(2026, 7, 28, tzinfo=UTC)


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


def _raid(rid: str) -> RaidItem:
    return RaidItem(
        id=rid, type=RaidType.RISK, title=rid, description="d",
        probability=3, impact=3, mitigation="m",
        provenance=RaidProvenance(
            reasoning="r", confidence=Confidence.HIGH,
            evidence=PrdEvidence(source_quote="q"), agent="raid", model="m", timestamp=NOW,
        ),
    )


def _plan(tasks, deps=None, constraints=None, raid=None) -> Plan:
    return Plan(
        id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")],
        tasks=tasks, dependencies=deps or [], constraints=constraints or [], raid=raid or [],
    )


def _facts(base: Plan, current: Plan, **kw):
    comparison = compare_versions(base, current, start_date=MONDAY)
    return assemble_status(
        comparison, baseline_raid=base.raid, current_raid=current.raid,
        period_label="this week", **kw,
    )


# A -> B, both critical.
def _base() -> Plan:
    return _plan([_task("A", 5), _task("B", 3)], [_dep("A", "B")])


# --- health rules (acceptance criteria) ------------------------------------


def test_no_change_is_green():
    facts = _facts(_base(), _base())
    assert facts.health is Health.GREEN
    assert facts.launch_shift_days == 0
    assert facts.is_on_track


def test_critical_path_slippage_flips_health_and_explains_why():
    """AC: a week with critical-path slippage flips the indicator by rule."""
    current = _plan([_task("A", 5), _task("B", 6)], [_dep("A", "B")])  # B +3 on the critical path
    facts = _facts(_base(), current)
    assert facts.health is Health.YELLOW  # slipped but under the red threshold
    assert facts.launch_shift_days == 3
    assert any("slipped 3 working day" in r for r in facts.health_reasons)


def test_missed_deadline_is_red():
    late = Constraint(
        id="con-x", type=ConstraintType.HARD_DATE, description="by",
        hard_date=date(2026, 8, 6), applies_to=["B"], provenance=_prov(),
    )
    base = _plan([_task("A", 5), _task("B", 3)], [_dep("A", "B")], [late])  # misses the date
    # Baseline met the date (shorter), current misses it.
    baseline = _plan([_task("A", 1), _task("B", 1)], [_dep("A", "B")], [late])
    facts = _facts(baseline, base)
    assert facts.health is Health.RED
    assert facts.breaches and any("missed" in r for r in facts.health_reasons)


def test_big_slip_is_red_by_threshold():
    current = _plan([_task("A", 5), _task("B", 15)], [_dep("A", "B")])  # +12 >= 10
    facts = _facts(_base(), current)
    assert facts.health is Health.RED


# --- traceability: facts come from the diff --------------------------------


def test_facts_are_traceable_to_diff_entries():
    current = _plan([_task("A", 5), _task("B", 6), _task("C", 2)], [_dep("A", "B")])
    facts = _facts(_base(), current)
    # B slipped (schedule delta), C added (structural).
    assert [s.id for s in facts.slipped] == ["B"]
    assert facts.structural_change_count >= 1  # C added


def test_new_raid_item_is_reported_and_yellows():
    current = _plan([_task("A", 5), _task("B", 3)], [_dep("A", "B")], raid=[_raid("r-new")])
    facts = _facts(_base(), current)
    assert [r.id for r in facts.raid_added] == ["r-new"]
    assert facts.health is Health.YELLOW


# --- narrative + renderers -------------------------------------------------


def test_fallback_narrative_speaks_only_from_facts():
    current = _plan([_task("A", 5), _task("B", 6)], [_dep("A", "B")])
    facts = _facts(_base(), current)
    narrative = fallback_narrative(facts)
    assert "at some risk" in narrative.exec_summary
    assert any("slipped 3 working day" in p for p in narrative.points)


def test_fallback_narrative_names_the_absent_baseline():
    """"Nothing to compare" and "nothing changed" mean opposite things (RC1-317):
    a quiet week against a committed baseline reports no material changes, but a
    period with no baseline must say so instead of claiming a comparison it
    never made."""
    no_baseline = fallback_narrative(_facts(_base(), _base()))  # baseline_version=None
    text = " ".join([no_baseline.exec_summary, *no_baseline.points]).lower()
    assert "no baseline" in text and "nothing to compare" in text
    assert "material changes" not in text
    assert "holds" not in no_baseline.exec_summary, "no baseline to hold against"

    committed = fallback_narrative(_facts(_base(), _base(), baseline_version=3))
    assert any("No material changes" in p for p in committed.points)
    assert "holds" in committed.exec_summary


def test_renderers_include_health_and_summary():
    facts = _facts(_base(), _base())
    md = render_markdown(facts, fallback_narrative(facts))
    html = render_html(facts, fallback_narrative(facts))
    assert "Status update — this week" in md
    assert "On track" in md
    assert "<div" in html and "On track" in html
