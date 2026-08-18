"""RC1-291 — the `plan spec review` verb: exit codes, JSON, and the free path."""

from __future__ import annotations

import json

from app.cli import main
from planner_core.spec_gate import SpecReview

VAGUE = "fixtures/spec-gate/vague-spec.md"
GOOD = "fixtures/spec-gate/good-spec.md"


def test_structural_only_reviews_the_vague_spec_without_a_credential(capsys):
    assert main(["spec", "review", VAGUE, "--structural-only"]) == 0
    out = capsys.readouterr().out
    assert "ADVISORY" in out
    assert "missing-requirement-ids" in out
    assert "Success metrics: TBD." in out


def test_structural_only_good_spec_is_clean(capsys):
    assert main(["spec", "review", GOOD, "--structural-only"]) == 0
    out = capsys.readouterr().out
    assert "0 structural" in out
    assert "clean review is a valid, complete answer" in out


def test_json_output_parses_back_into_the_model(capsys):
    assert main(["spec", "review", VAGUE, "--structural-only", "--json"]) == 0
    review = SpecReview.model_validate(json.loads(capsys.readouterr().out))
    assert review.source_document == VAGUE
    assert review.verdict.value == "advisory"
    assert len(review.structural_findings) == 5


def test_missing_file_exits_2(capsys):
    assert main(["spec", "review", "no/such/spec.md", "--structural-only"]) == 2
    assert "not found" in capsys.readouterr().err


def test_unknown_fail_on_category_exits_2(capsys):
    assert main(["spec", "review", VAGUE, "--structural-only", "--fail-on", "nonsense"]) == 2
    err = capsys.readouterr().err
    assert "unknown --fail-on category" in err
    assert "conflicting_requirement" in err  # the valid values are listed


def test_fail_on_gates_only_on_surviving_rubric_findings(capsys, monkeypatch):
    """--fail-on with a fake agent: a blocked category flips the exit code."""
    from datetime import UTC, datetime

    import agents as agents_pkg
    from planner_core.provenance import Confidence, Provenance
    from planner_core.spec_gate import FindingCategory, SpecFinding, SpecSeverity

    quote = "All users will be cut over in a single weekend migration"

    class FakeAgent:
        def __init__(self, *, model=None, client=None):
            pass

        def run(self, spec_text, structural):
            return SpecReview(
                structural_findings=list(structural),
                findings=[
                    SpecFinding(
                        category=FindingCategory.CONFLICTING_REQUIREMENT,
                        severity=SpecSeverity.BLOCKER,
                        suggested_rewrite=None,
                        provenance=Provenance(
                            reasoning="conflicts with the 90-day requirement",
                            source_quote=quote,
                            source_section="Rollout",
                            confidence=Confidence.HIGH,
                            agent="spec-review",
                            model="claude-test",
                            timestamp=datetime(2026, 8, 18, tzinfo=UTC),
                        ),
                    )
                ],
                rubric_version=1,
            )

    monkeypatch.setattr(agents_pkg, "SpecReviewAgent", FakeAgent)

    assert main(["spec", "review", VAGUE, "--fail-on", "conflicting_requirement"]) == 1
    capsys.readouterr()
    # Same review, ungated category configured: advisory exit.
    assert main(["spec", "review", VAGUE, "--fail-on", "unowned_scope"]) == 0
    assert "BLOCKED" not in capsys.readouterr().out
