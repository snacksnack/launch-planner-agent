"""RC1-285 — the spec-gate finding model and its enforced quote rule."""

from datetime import UTC, datetime

import pytest
from planner_core.provenance import Confidence, Provenance
from planner_core.spec_gate import (
    FindingCategory,
    SpecFinding,
    SpecReview,
    SpecSeverity,
    SpecVerdict,
    StructuralFinding,
)
from pydantic import ValidationError


def _provenance(quote: str = "The system must be fast.") -> Provenance:
    return Provenance(
        reasoning="'fast' has no percentile, population, or measurement window.",
        source_quote=quote,
        source_section="Requirements",
        confidence=Confidence.HIGH,
        agent="spec-review",
        model="claude-sonnet-5",
        timestamp=datetime(2026, 8, 18, tzinfo=UTC),
    )


def _finding(**overrides) -> SpecFinding:
    kwargs = {
        "category": FindingCategory.AMBIGUOUS_QUANTIFIER,
        "severity": SpecSeverity.WARNING,
        "provenance": _provenance(),
    }
    kwargs.update(overrides)
    return SpecFinding(**kwargs)


def test_finding_construction_and_single_sourced_properties():
    finding = _finding(suggested_rewrite="p95 latency under 300ms at 1k rps.")
    assert finding.quote == "The system must be fast."
    assert finding.section == "Requirements"
    assert finding.explanation.startswith("'fast'")
    # Properties read straight from provenance — there is no second copy to drift.
    assert finding.quote is finding.provenance.source_quote


def test_finding_without_provenance_is_unconstructible():
    with pytest.raises(ValidationError):
        SpecFinding(
            category=FindingCategory.MISSING_NFR,
            severity=SpecSeverity.BLOCKER,
        )


def test_empty_quote_rejected_by_provenance_itself():
    with pytest.raises(ValidationError):
        _provenance(quote="")


def test_whitespace_only_quote_rejected():
    with pytest.raises(ValidationError, match="whitespace is not a quote"):
        _finding(provenance=_provenance(quote="   \n\t "))


def test_round_trip_serialization():
    review = SpecReview(
        source_document="fixtures/spec-gate/vague-spec.md",
        findings=[_finding()],
        structural_findings=[
            StructuralFinding(
                code="missing-section",
                severity=SpecSeverity.WARNING,
                message="no 'Non-goals' section found",
            )
        ],
        questions_for_author=["What latency target, at which percentile?"],
        readiness_score=0.7,
    )
    restored = SpecReview.model_validate_json(review.model_dump_json())
    assert restored == review


def test_zero_findings_is_a_valid_clean_review():
    review = SpecReview()
    assert review.findings == []
    assert review.structural_findings == []
    assert review.verdict is SpecVerdict.ADVISORY
    assert review.readiness_score is None


def test_structural_finding_needs_no_quote_but_a_spec_finding_always_has_one():
    absence = StructuralFinding(
        code="missing-section",
        severity=SpecSeverity.WARNING,
        message="no 'Rollback' section found",
    )
    assert absence.quote is None
    assert _finding().quote  # the LLM shape cannot exist without one


def test_findings_sort_worst_first():
    review = SpecReview(
        findings=[
            _finding(severity=SpecSeverity.NIT),
            _finding(severity=SpecSeverity.BLOCKER),
            _finding(severity=SpecSeverity.WARNING),
        ]
    )
    assert [f.severity for f in review.sorted_findings] == [
        SpecSeverity.BLOCKER,
        SpecSeverity.WARNING,
        SpecSeverity.NIT,
    ]
    assert len(review.blockers) == 1


def test_json_schema_is_exportable_for_schema_forcing():
    schema = SpecReview.model_json_schema()
    assert "SpecFinding" in schema.get("$defs", {})
    category = schema["$defs"]["FindingCategory"]
    assert set(category["enum"]) == {c.value for c in FindingCategory}


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        SpecReview(unexpected="nope")
