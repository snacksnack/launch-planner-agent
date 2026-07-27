"""Tests for the RAID Agent using an injected fake Anthropic client."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agents import ProposedRaidItem, ProposedRaidLog, RaidAgent
from agents.raid import build_user_prompt
from agents.schema import ProposedRaidProvenance
from planner_core import (
    Confidence,
    PrdEvidence,
    RaidType,
    ScheduleEvidence,
    ScheduleFact,
    TeamMember,
)

TEAM = [TeamMember(id="tm-1", name="Ada", role="Lead")]
FACTS = [
    ScheduleFact(
        code="single-owner-critical-path",
        statement="Ada owns 3 of 5 critical-path tasks.",
        entity_ids=["tm-1", "task-a", "task-b"],
        severity_hint="high",
    )
]
PRD = "The critical plugins have no direct Cloud equivalent."


def _pp(evidence) -> ProposedRaidProvenance:
    return ProposedRaidProvenance(
        reasoning="because", confidence=Confidence.HIGH, evidence=evidence
    )


def _proposal() -> ProposedRaidLog:
    return ProposedRaidLog(
        items=[
            ProposedRaidItem(
                id="raid-1", type=RaidType.RISK, title="Key-person risk",
                description="Single owner on the critical path.",
                probability=3, impact=4, mitigation="cross-train", suggested_owner_id="tm-1",
                rationale=None,
                provenance=_pp(
                    ScheduleEvidence(
                        fact_code="single-owner-critical-path",
                        statement="Ada owns 3 of 5 critical-path tasks.",
                        entity_ids=["tm-1", "task-a", "task-b"],
                    )
                ),
            ),
            ProposedRaidItem(
                id="raid-2", type=RaidType.ASSUMPTION, title="Plugin gap",
                description="Plugins may not port.",
                probability=None, impact=None, mitigation=None, suggested_owner_id=None,
                rationale=None,
                provenance=_pp(
                    PrdEvidence(
                        source_quote="no direct Cloud equivalent", source_section="Scope"
                    )
                ),
            ),
        ]
    )


class _FakeMessages:
    def __init__(self, proposal):
        self._proposal = proposal
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self._proposal)


class _FakeClient:
    def __init__(self, proposal):
        self.messages = _FakeMessages(proposal)


def _agent() -> RaidAgent:
    return RaidAgent(
        model="claude-test",
        client=_FakeClient(_proposal()),
        now=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
    )


def test_run_stamps_run_facts_and_preserves_evidence():
    items = _agent().run(PRD, FACTS, TEAM)
    assert [i.id for i in items] == ["raid-1", "raid-2"]

    risk = items[0]
    assert risk.type is RaidType.RISK
    assert risk.severity == 12  # 3 x 4
    # Run facts are stamped by Python, not the model.
    assert risk.provenance.agent == "raid"
    assert risk.provenance.model == "claude-test"
    assert risk.provenance.timestamp == datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    # Schedule evidence is carried through.
    assert risk.provenance.evidence.kind == "schedule"
    assert risk.provenance.evidence.fact_code == "single-owner-critical-path"

    assumption = items[1]
    assert assumption.provenance.evidence.kind == "prd"
    assert assumption.provenance.evidence.source_quote == "no direct Cloud equivalent"


def test_schema_forces_the_proposal_model():
    agent = _agent()
    agent.run(PRD, FACTS, TEAM)
    (call,) = agent._client.messages.calls
    assert call["output_format"] is ProposedRaidLog


def test_user_prompt_includes_roster_facts_and_prd():
    prompt = build_user_prompt("PRD BODY", FACTS, TEAM)
    assert "tm-1" in prompt
    assert "single-owner-critical-path" in prompt  # the fact code is offered to cite
    assert "PRD BODY" in prompt


def test_proposal_schema_is_publishable():
    schema = ProposedRaidLog.model_json_schema()
    assert "ProposedRaidItem" in schema["$defs"]
