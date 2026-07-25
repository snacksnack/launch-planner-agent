"""Tests for the Dependency Agent using an injected fake Anthropic client."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agents import (
    DependencyAgent,
    ProposedDependencies,
    ProposedDependency,
    ProposedProvenance,
)
from agents.dependency import build_user_prompt
from planner_core import (
    Confidence,
    Constraint,
    ConstraintType,
    DependencyType,
    Milestone,
    Provenance,
    Task,
    ThreePointEstimate,
)

LEGAL_QUOTE = "Legal has to sign off before any client data moves to Cloud"


def _task(tid: str) -> Task:
    return Task(
        id=tid,
        name=tid,
        estimate=ThreePointEstimate(optimistic=1, likely=1, pessimistic=1),
        provenance=Provenance(
            reasoning="r",
            source_quote="q",
            source_section=None,
            confidence=Confidence.HIGH,
            agent="work-breakdown",
            model="m",
            timestamp=datetime(2026, 7, 24, tzinfo=UTC),
        ),
    )


TASKS = [_task("task-legal"), _task("task-data"), _task("task-a"), _task("task-b")]
CONSTRAINTS = [
    Constraint(
        id="con-legal",
        type=ConstraintType.GATE,
        description="legal signoff",
        gate="Legal sign-off",
        applies_to=["task-data"],
        provenance=TASKS[0].provenance,
    )
]
MILESTONES = [Milestone(id="ms-done", name="All done", provenance=TASKS[0].provenance)]


def _pp(quote: str) -> ProposedProvenance:
    return ProposedProvenance(
        reasoning="because", source_quote=quote, source_section=None, confidence=Confidence.HIGH
    )


def _edge(pred: str, succ: str, quote: str = "q") -> ProposedDependency:
    return ProposedDependency(
        predecessor_id=pred,
        successor_id=succ,
        type=DependencyType.FINISH_TO_START,
        lag=0.0,
        provenance=_pp(quote),
    )


def _proposal() -> ProposedDependencies:
    return ProposedDependencies(
        dependencies=[
            _edge("task-legal", "task-data", LEGAL_QUOTE),  # gate edge (AC2)
            _edge("task-a", "task-b"),  # ok
            _edge("task-a", "task-ghost"),  # dangling
            _edge("task-a", "task-a"),  # self-loop
            _edge("task-a", "task-b"),  # duplicate
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


def _agent() -> DependencyAgent:
    return DependencyAgent(
        model="claude-test",
        client=_FakeClient(_proposal()),
        now=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )


def test_invalid_edges_are_rejected_and_never_stamped():
    result = _agent().run("prd", TASKS, CONSTRAINTS)
    assert len(result.dependencies) == 2
    codes = sorted(r.code for r in result.rejections)
    assert codes == ["dangling-reference", "duplicate-edge", "self-loop"]


def test_gate_edge_is_preserved_with_verbatim_quote_and_stamped_facts():
    result = _agent().run("prd", TASKS, CONSTRAINTS)
    gate_edge = next(
        d for d in result.dependencies
        if d.predecessor_id == "task-legal" and d.successor_id == "task-data"
    )
    assert gate_edge.provenance.source_quote == LEGAL_QUOTE
    # Run facts are stamped by Python, not the model.
    assert gate_edge.provenance.agent == "dependency"
    assert gate_edge.provenance.model == "claude-test"
    assert gate_edge.provenance.timestamp == datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    # Ids are assigned sequentially over accepted edges.
    assert [d.id for d in result.dependencies] == ["dep-1", "dep-2"]


def _cycle_edge(pred: str, succ: str, confidence: Confidence) -> ProposedDependency:
    pp = ProposedProvenance(
        reasoning="because", source_quote="q", source_section=None, confidence=confidence
    )
    return ProposedDependency(
        predecessor_id=pred, successor_id=succ, type=DependencyType.FINISH_TO_START, lag=0.0,
        provenance=pp,
    )


def test_agent_breaks_a_proposed_cycle():
    proposal = ProposedDependencies(
        dependencies=[
            _cycle_edge("task-a", "task-b", Confidence.HIGH),
            _cycle_edge("task-b", "task-a", Confidence.LOW),  # weakest -> removed
        ]
    )
    agent = DependencyAgent(
        model="m", client=_FakeClient(proposal), now=datetime(2026, 7, 24, tzinfo=UTC)
    )
    result = agent.run("prd", TASKS, CONSTRAINTS)

    assert len(result.dependencies) == 1
    assert result.dependencies[0].predecessor_id == "task-a"
    assert len(result.cycle_breaks) == 1
    assert result.cycle_breaks[0].predecessor_id == "task-b"


def test_schema_forces_the_proposal_model():
    agent = _agent()
    agent.run("prd", TASKS, CONSTRAINTS)
    (call,) = agent._client.messages.calls
    assert call["output_format"] is ProposedDependencies


def test_user_prompt_lists_task_milestone_and_constraint_ids():
    prompt = build_user_prompt("PRD BODY", TASKS, CONSTRAINTS, MILESTONES)
    assert "task-legal" in prompt and "task-data" in prompt
    assert "con-legal" in prompt
    assert "ms-done" in prompt  # milestones offered as linkable endpoints (RC1-198)
    assert "PRD BODY" in prompt


def test_task_to_milestone_edge_survives_filtering():
    # A milestone id is a valid endpoint, so a task -> milestone link is accepted
    # and stamped; an edge to an unknown milestone is dropped as dangling (RC1-198).
    proposal = ProposedDependencies(
        dependencies=[
            _edge("task-a", "ms-done"),  # task -> milestone: valid
            _edge("task-a", "ms-ghost"),  # unknown milestone: dangling
        ]
    )
    agent = DependencyAgent(
        model="m", client=_FakeClient(proposal), now=datetime(2026, 7, 24, tzinfo=UTC)
    )
    result = agent.run("prd", TASKS, CONSTRAINTS, MILESTONES)

    assert [(d.predecessor_id, d.successor_id) for d in result.dependencies] == [
        ("task-a", "ms-done")
    ]
    assert [r.code for r in result.rejections] == ["dangling-reference"]


def test_proposal_schema_is_publishable():
    schema = ProposedDependencies.model_json_schema()
    assert "ProposedDependency" in schema["$defs"]
    props = schema["$defs"]["ProposedDependency"]["properties"]
    assert {"predecessor_id", "successor_id", "type", "lag", "provenance"} <= set(props)
