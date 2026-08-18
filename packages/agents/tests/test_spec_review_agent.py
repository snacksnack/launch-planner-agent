"""Tests for the Spec Review Agent using an injected fake Anthropic client.

No credentials or network: a fake client returns a canned `ProposedSpecReview`,
and we assert stamping, structural-context injection, invalid-finding dropping,
and rubric-version attribution. Live rubric quality is measured by the evals
(RC1-292), not here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agents import (
    RUBRIC_VERSION,
    ProposedProvenance,
    ProposedSpecFinding,
    ProposedSpecReview,
    SpecReviewAgent,
)
from agents.spec_review import SYSTEM_PROMPT, build_user_prompt
from planner_core import Confidence
from planner_core.spec_gate import FindingCategory, SpecSeverity, StructuralFinding

STRUCTURAL = [
    StructuralFinding(
        code="missing-section",
        severity=SpecSeverity.WARNING,
        message="no section matching 'acceptance criteria' found",
    )
]


def _proposed_finding(quote: str = "The login flow must be fast") -> ProposedSpecFinding:
    return ProposedSpecFinding(
        category=FindingCategory.AMBIGUOUS_QUANTIFIER,
        severity=SpecSeverity.WARNING,
        suggested_rewrite="p95 login under 2 seconds.",
        provenance=ProposedProvenance(
            reasoning="no percentile or measurement point",
            source_quote=quote,
            source_section="Goals",
            confidence=Confidence.HIGH,
        ),
    )


def _proposal(*findings: ProposedSpecFinding) -> ProposedSpecReview:
    return ProposedSpecReview(
        findings=list(findings) or [_proposed_finding()],
        questions_for_author=["What latency target, at which percentile?"],
    )


class _FakeMessages:
    def __init__(self, proposal: ProposedSpecReview):
        self._proposal = proposal
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self._proposal)


class _FakeClient:
    def __init__(self, proposal: ProposedSpecReview):
        self.messages = _FakeMessages(proposal)


def test_agent_stamps_run_facts_and_records_rubric_version():
    ts = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    agent = SpecReviewAgent(model="claude-test", client=_FakeClient(_proposal()), now=ts)

    review = agent.run("... spec ...", STRUCTURAL)

    (finding,) = review.findings
    # Content came from the proposal…
    assert finding.quote == "The login flow must be fast"
    assert finding.category is FindingCategory.AMBIGUOUS_QUANTIFIER
    # …but agent/model/timestamp are stamped by Python, not the model.
    assert finding.provenance.agent == "spec-review"
    assert finding.provenance.model == "claude-test"
    assert finding.provenance.timestamp == ts
    assert review.rubric_version == RUBRIC_VERSION
    # Structural findings pass through so the draft review is complete.
    assert review.structural_findings == STRUCTURAL
    assert review.questions_for_author == ["What latency target, at which percentile?"]


def test_agent_schema_forces_the_proposal_model():
    client = _FakeClient(_proposal())
    SpecReviewAgent(client=client, now=datetime(2026, 8, 18, tzinfo=UTC)).run("spec", [])
    (call,) = client.messages.calls
    assert call["output_format"] is ProposedSpecReview
    assert call["system"] == SYSTEM_PROMPT
    assert call["messages"][0]["role"] == "user"


def test_structural_findings_are_injected_as_recorded_context():
    prompt = build_user_prompt("MY SPEC BODY", STRUCTURAL)
    assert "already recorded" in prompt
    assert "missing-section" in prompt
    assert "MY SPEC BODY" in prompt


def test_no_structural_findings_says_so_rather_than_omitting_the_block():
    prompt = build_user_prompt("spec", [])
    assert "deterministic checks found nothing" in prompt


def test_whitespace_quote_finding_is_dropped_and_counted_not_raised():
    good = _proposed_finding()
    bad = _proposed_finding(quote="   ")
    agent = SpecReviewAgent(
        client=_FakeClient(_proposal(good, bad)), now=datetime(2026, 8, 18, tzinfo=UTC)
    )

    review = agent.run("spec", [])

    assert len(review.findings) == 1  # the good one survived
    assert agent.last_dropped_invalid == 1  # the bad one is counted, not hidden


def test_verdict_and_score_are_not_set_by_the_agent():
    """RC1-290 owns those; the agent must leave the defaults untouched."""
    review = SpecReviewAgent(
        client=_FakeClient(_proposal()), now=datetime(2026, 8, 18, tzinfo=UTC)
    ).run("spec", [])
    assert review.readiness_score is None
    assert review.verdict.value == "advisory"


def test_empty_findings_is_a_valid_complete_answer():
    proposal = ProposedSpecReview(findings=[], questions_for_author=[])
    review = SpecReviewAgent(
        client=_FakeClient(proposal), now=datetime(2026, 8, 18, tzinfo=UTC)
    ).run("a genuinely good spec", [])
    assert review.findings == []
    assert review.rubric_version == RUBRIC_VERSION


def test_default_model_comes_from_env(monkeypatch):
    monkeypatch.setenv("LPA_ANTHROPIC_MODEL", "claude-from-env")
    review = SpecReviewAgent(
        client=_FakeClient(_proposal()), now=datetime(2026, 8, 18, tzinfo=UTC)
    ).run("spec", [])
    assert review.findings[0].provenance.model == "claude-from-env"


def test_rubric_names_every_category():
    for category in FindingCategory:
        assert category.value in SYSTEM_PROMPT