"""Tests for the deterministic breakdown validation (no LLM in the loop)."""

from __future__ import annotations

from datetime import UTC, datetime

from planner_core import (
    Confidence,
    Epic,
    Plan,
    Provenance,
    Severity,
    Task,
    TeamMember,
    ThreePointEstimate,
    WorkBreakdown,
    build_report,
    coverage_gaps,
    markdown_sections,
)
from planner_core.validation import flag_unverifiable_quotes

PRD = """\
# Project Skyline

## Goals
Migrate all projects to the cloud by Q4.

## Team & budget
Bring on two contractors to run the migration.

## Risks
Plugins may not be compatible.
"""


def _prov(quote: str, *, section: str | None = "Goals", confidence: Confidence = Confidence.HIGH):
    return Provenance(
        reasoning="because the PRD says so",
        source_quote=quote,
        source_section=section,
        confidence=confidence,
        agent="work-breakdown",
        model="golden-baseline",
        timestamp=datetime(2026, 7, 23, tzinfo=UTC),
    )


def _est(o=1.0, m=2.0, p=3.0) -> ThreePointEstimate:
    return ThreePointEstimate(optimistic=o, likely=m, pessimistic=p)


def _plan(*, tasks, epics=None, team=None) -> Plan:
    return Plan(
        id="p",
        name="Skyline",
        team=team if team is not None else [TeamMember(id="tm-1", name="Ada")],
        epics=epics if epics is not None else [],
        tasks=tasks,
    )


def _task(**overrides) -> Task:
    base = dict(
        id="task-1",
        name="Migrate projects",
        owner_id="tm-1",
        estimate=_est(),
        provenance=_prov("Migrate all projects to the cloud by Q4."),
    )
    base.update(overrides)
    return Task(**base)


def test_clean_plan_reports_ok_with_no_errors():
    report = build_report(_plan(tasks=[_task()]), PRD)
    assert report.ok
    assert report.errors == []
    assert report.task_count == 1


def test_unknown_owner_is_an_error():
    report = build_report(_plan(tasks=[_task(owner_id="tm-nope")]), PRD)
    assert not report.ok
    assert [i.code for i in report.errors] == ["unknown-owner"]


def test_unknown_epic_is_an_error():
    report = build_report(_plan(tasks=[_task(epic_id="epic-ghost")]), PRD)
    assert any(i.code == "unknown-epic" and i.severity is Severity.ERROR for i in report.errors)


def test_unassigned_task_is_a_warning_not_an_error():
    report = build_report(_plan(tasks=[_task(owner_id=None)]), PRD)
    assert report.ok  # still acceptable
    assert any(i.code == "unassigned-task" for i in report.warnings)


def test_low_confidence_is_flagged():
    prov = _prov("Migrate all projects to the cloud by Q4.", confidence=Confidence.LOW)
    report = build_report(_plan(tasks=[_task(provenance=prov)]), PRD)
    assert any(i.code == "low-confidence" for i in report.warnings)


def test_hallucinated_quote_is_flagged():
    task = _task(provenance=_prov("Rewrite everything in Rust immediately."))
    report = build_report(_plan(tasks=[task]), PRD)
    assert any(i.code == "unverifiable-quote" for i in report.warnings)


def test_quote_matching_ignores_prd_line_wrapping():
    # A quote that spans a wrapped line in the PRD still matches after normalization.
    prov = _prov("Bring on two contractors to run the migration.", section="Team & budget")
    report = build_report(_plan(tasks=[_task(provenance=prov)]), PRD)
    assert not any(i.code == "unverifiable-quote" for i in report.warnings)


def test_markdown_sections_and_coverage_gaps():
    assert markdown_sections(PRD) == ["Project Skyline", "Goals", "Team & budget", "Risks"]
    # Only "Goals" is cited by the single task; the rest are gaps.
    gaps = coverage_gaps(_plan(tasks=[_task()]), PRD)
    assert "Goals" not in gaps
    assert "Risks" in gaps and "Team & budget" in gaps


def test_work_breakdown_container_round_trips():
    prov = _prov("Migrate all projects to the cloud by Q4.")
    wb = WorkBreakdown(
        epics=[Epic(id="epic-1", name="Migration", provenance=prov)],
        tasks=[_task(epic_id="epic-1")],
    )
    assert WorkBreakdown.model_validate_json(wb.model_dump_json()) == wb


# --- RC1-257: quote matching ignores markdown decoration -------------------


def _quoting(quote: str) -> Plan:
    return _plan(
        tasks=[
            Task(id="t-1", name="Do the thing", estimate=_est(), provenance=_prov(quote)),
        ]
    )


def test_a_quote_wrapped_in_markdown_emphasis_is_verifiable():
    """RC1-257, found by running the agent rather than by a passing test.

    The shipped `product-launch` PRD bolds whole clauses, and the agent quotes
    what a reader sees rather than the markup — the right reading of "copied
    verbatim from the PRD". Three faithful quotes were being reported as
    hallucinated provenance.
    """
    prd = (
        "## Goals\n\nAurora collects data, and **the privacy review has to be\n"
        "signed off first.**\n"
    )
    quote = "the privacy review has to be signed off first."
    assert not flag_unverifiable_quotes(_quoting(quote), prd), (
        "markdown emphasis is decoration, not part of the quote"
    )


def test_a_quote_the_prd_does_not_contain_is_still_flagged():
    """The decoration fix must not become a licence to invent."""
    prd = "## Goals\n\nAurora collects data, and **the privacy review has to be signed off.**\n"
    assert flag_unverifiable_quotes(_quoting("the security review has to be signed off."), prd)


def test_bold_transliterated_as_a_double_quote_is_tolerated():
    """`...the public launch** and Apple's...` came back as `...launch" and Apple's...`."""
    prd = (
        "## Goals\n\nIt is a mobile app, so **App Store approval is required\n"
        "before launch** and timing slips.\n"
    )
    quote = 'App Store approval is required before launch" and timing slips.'
    assert not flag_unverifiable_quotes(_quoting(quote), prd)


def test_bold_transliterated_as_an_em_dash_is_tolerated():
    """RC1-326: the same closing `**` also comes back as ` — `.

    Found the same way as RC1-257 — by rerunning the flapping `product-launch`
    case and reading the quotes, not from a theory.
    """
    prd = (
        "## Goals\n\nIt is a mobile app, so **App Store approval is required\n"
        "before launch** and timing slips.\n"
    )
    quote = "App Store approval is required before launch — and timing slips."
    assert not flag_unverifiable_quotes(_quoting(quote), prd)


def test_a_lowercased_leading_capital_is_tolerated():
    """RC1-326: the agent lowercases a capital when quoting mid-sentence."""
    prd = "## Scope\n\nThe v1.0 feature set still has a couple of screens to finish.\n"
    quote = "the v1.0 feature set still has a couple of screens to finish."
    assert not flag_unverifiable_quotes(_quoting(quote), prd)
