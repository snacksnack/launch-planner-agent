"""RC1-290 — quote verification drops, the score recomputes by hand, the
default verdict never blocks."""

from datetime import UTC, datetime
from pathlib import Path

from planner_core.provenance import Confidence, Provenance
from planner_core.spec_gate import (
    WEIGHTS,
    FindingCategory,
    SpecFinding,
    SpecReview,
    SpecSeverity,
    SpecVerdict,
    StructuralFinding,
    finalize_review,
    readiness_score,
    render_review_markdown,
    verify_quotes,
)

SPEC_GATE = Path(__file__).resolve().parents[3] / "fixtures" / "spec-gate"
VAGUE = (SPEC_GATE / "vague-spec.md").read_text()


def _finding(
    quote: str,
    category: FindingCategory = FindingCategory.AMBIGUOUS_QUANTIFIER,
    severity: SpecSeverity = SpecSeverity.WARNING,
) -> SpecFinding:
    return SpecFinding(
        category=category,
        severity=severity,
        suggested_rewrite=None,
        provenance=Provenance(
            reasoning="test",
            source_quote=quote,
            source_section=None,
            confidence=Confidence.HIGH,
            agent="spec-review",
            model="claude-test",
            timestamp=datetime(2026, 8, 18, tzinfo=UTC),
        ),
    )


REAL = "The login flow must be fast"  # verbatim in the vague spec
FABRICATED = "The system shall guarantee five nines of availability"  # not in it


def test_fabricated_quote_is_dropped_and_counted():
    review = SpecReview(findings=[_finding(REAL), _finding(FABRICATED)])
    verified = verify_quotes(review, VAGUE)
    assert [f.quote for f in verified.findings] == [REAL]
    assert verified.dropped_unverifiable == 1


def test_decorated_or_reflowed_quote_still_verifies():
    # The source has no ** around this text; a faithful quote that *adds* none
    # but arrives wrapped/emphasized must still match through the normalizer.
    wrapped = "The login flow\nmust be **fast**"
    verified = verify_quotes(SpecReview(findings=[_finding(wrapped)]), VAGUE)
    assert verified.dropped_unverifiable == 0


def test_readiness_score_is_recomputable_by_hand():
    review = SpecReview(
        findings=[
            _finding(REAL, severity=SpecSeverity.BLOCKER),
            _finding(REAL, severity=SpecSeverity.WARNING),
        ],
        structural_findings=[
            StructuralFinding(code="missing-section", severity=SpecSeverity.NIT, message="m")
        ],
    )
    by_hand = 1.0 - (
        WEIGHTS[SpecSeverity.BLOCKER] + WEIGHTS[SpecSeverity.WARNING] + WEIGHTS[SpecSeverity.NIT]
    )
    assert readiness_score(review) == round(by_hand, 4)


def test_score_floors_at_zero():
    review = SpecReview(findings=[_finding(REAL, severity=SpecSeverity.BLOCKER)] * 5)
    assert readiness_score(review) == 0.0


def test_default_configuration_never_blocks():
    review = SpecReview(
        findings=[
            _finding(
                REAL,
                category=FindingCategory.CONFLICTING_REQUIREMENT,
                severity=SpecSeverity.BLOCKER,
            )
        ]
    )
    assert finalize_review(review, VAGUE).verdict is SpecVerdict.ADVISORY


def test_block_on_gates_on_category_not_severity():
    nit_conflict = _finding(
        REAL, category=FindingCategory.CONFLICTING_REQUIREMENT, severity=SpecSeverity.NIT
    )
    blocker_quantifier = _finding(
        REAL, category=FindingCategory.AMBIGUOUS_QUANTIFIER, severity=SpecSeverity.BLOCKER
    )
    block_on = {FindingCategory.CONFLICTING_REQUIREMENT}

    review = finalize_review(SpecReview(findings=[nit_conflict]), VAGUE, block_on)
    assert review.verdict is SpecVerdict.BLOCKED  # nit severity, gated category

    review = finalize_review(SpecReview(findings=[blocker_quantifier]), VAGUE, block_on)
    assert review.verdict is SpecVerdict.ADVISORY  # blocker severity, ungated category


def test_a_dropped_finding_cannot_block_or_depress_the_score():
    fabricated_conflict = _finding(
        FABRICATED,
        category=FindingCategory.CONFLICTING_REQUIREMENT,
        severity=SpecSeverity.BLOCKER,
    )
    review = finalize_review(
        SpecReview(findings=[fabricated_conflict]),
        VAGUE,
        {FindingCategory.CONFLICTING_REQUIREMENT},
    )
    assert review.verdict is SpecVerdict.ADVISORY
    assert review.readiness_score == 1.0
    assert review.dropped_unverifiable == 1


def test_renderer_and_json_come_from_the_same_object():
    review = finalize_review(
        SpecReview(
            source_document="fixtures/spec-gate/vague-spec.md",
            findings=[_finding(REAL), _finding(FABRICATED)],
            questions_for_author=["What latency target, at which percentile?"],
        ),
        VAGUE,
    )
    md = render_review_markdown(review)
    assert "ADVISORY" in md
    assert REAL in md
    assert "1 dropped" in md
    assert "What latency target" in md
    assert SpecReview.model_validate_json(review.model_dump_json()) == review


def test_clean_review_renders_as_a_valid_complete_answer():
    md = render_review_markdown(finalize_review(SpecReview(), VAGUE))
    assert "clean review is a valid, complete answer" in md
    assert "ADVISORY" in md
