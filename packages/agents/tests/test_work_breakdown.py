"""Tests for the Work Breakdown Agent using an injected fake Anthropic client.

No credentials or network: a fake client returns a canned `ProposedWorkBreakdown`,
and we assert the agent stamps provenance facts and produces canonical models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agents import (
    ProposedEpic,
    ProposedProvenance,
    ProposedTask,
    ProposedWorkBreakdown,
    WorkBreakdownAgent,
    build_user_prompt,
)
from planner_core import Confidence, TeamMember, ThreePointEstimate

TEAM = [
    TeamMember(id="tm-1", name="Ada Okoro", role="Backend Engineer"),
    TeamMember(id="tm-2", name="Sven Lindqvist", role="Data Migration Engineer"),
]


def _proposal() -> ProposedWorkBreakdown:
    prov = ProposedProvenance(
        reasoning="stated in the PRD",
        source_quote="Migrate all projects to the cloud by Q4.",
        source_section="Goals",
        confidence=Confidence.HIGH,
    )
    return ProposedWorkBreakdown(
        epics=[ProposedEpic(id="epic-1", name="Migration", description=None, provenance=prov)],
        tasks=[
            ProposedTask(
                id="task-1",
                name="Migrate projects",
                description=None,
                epic_id="epic-1",
                owner_id="tm-2",
                estimate=ThreePointEstimate(optimistic=2, likely=4, pessimistic=8),
                provenance=prov,
            )
        ],
    )


class _FakeMessages:
    def __init__(self, proposal: ProposedWorkBreakdown):
        self._proposal = proposal
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self._proposal)


class _FakeClient:
    def __init__(self, proposal: ProposedWorkBreakdown):
        self.messages = _FakeMessages(proposal)


def test_agent_stamps_run_facts_onto_provenance():
    ts = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    agent = WorkBreakdownAgent(model="claude-test", client=_FakeClient(_proposal()), now=ts)

    wb = agent.run("... prd ...", TEAM)

    assert [e.id for e in wb.epics] == ["epic-1"]
    task = wb.tasks[0]
    # Content came from the proposal…
    assert task.provenance.source_quote == "Migrate all projects to the cloud by Q4."
    assert task.owner_id == "tm-2"
    assert task.estimate.expected == (2 + 16 + 8) / 6
    # …but agent/model/timestamp are stamped by Python, not the model.
    assert task.provenance.agent == "work-breakdown"
    assert task.provenance.model == "claude-test"
    assert task.provenance.timestamp == ts
    assert wb.epics[0].provenance.model == "claude-test"


def test_agent_schema_forces_the_proposal_model():
    client = _FakeClient(_proposal())
    WorkBreakdownAgent(client=client, now=datetime(2026, 7, 23, tzinfo=UTC)).run("prd", TEAM)
    (call,) = client.messages.calls
    assert call["output_format"] is ProposedWorkBreakdown
    assert call["model"]  # a model id was passed
    assert call["messages"][0]["role"] == "user"


def test_user_prompt_lists_team_ids_and_includes_the_prd():
    prompt = build_user_prompt("MY PRD BODY", TEAM)
    assert "tm-1" in prompt and "tm-2" in prompt
    assert "MY PRD BODY" in prompt


def test_default_model_comes_from_env(monkeypatch):
    monkeypatch.setenv("LPA_ANTHROPIC_MODEL", "claude-from-env")
    ts = datetime(2026, 7, 23, tzinfo=UTC)
    wb = WorkBreakdownAgent(client=_FakeClient(_proposal()), now=ts).run("prd", TEAM)
    assert wb.tasks[0].provenance.model == "claude-from-env"


def test_proposal_schema_is_publishable():
    schema = ProposedWorkBreakdown.model_json_schema()
    assert schema["title"] == "ProposedWorkBreakdown"
    assert "ProposedTask" in schema["$defs"]
    # The reduced provenance must NOT ask the model for run facts.
    prov_props = schema["$defs"]["ProposedProvenance"]["properties"]
    assert "source_quote" in prov_props
    assert "timestamp" not in prov_props and "agent" not in prov_props
