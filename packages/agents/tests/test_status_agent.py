"""Tests for the Status Agent using an injected fake Anthropic client."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from agents import StatusAgent
from agents.status import build_user_prompt
from planner_core import Health, NamedChange, StatusFacts, StatusNarrative


def _facts() -> StatusFacts:
    return StatusFacts(
        period_label="Week of 2026-07-27",
        health=Health.YELLOW,
        health_reasons=["launch slipped 3 working day(s)"],
        launch_before=date(2026, 9, 21),
        launch_after=date(2026, 9, 24),
        launch_shift_days=3,
        slipped=[NamedChange(id="task-b", name="Bulk migration", shift_days=3)],
    )


class _FakeMessages:
    def __init__(self, out):
        self._out = out
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed_output=self._out)


class _FakeClient:
    def __init__(self, out):
        self.messages = _FakeMessages(out)


def test_run_returns_the_narrative_and_forces_the_schema():
    out = StatusNarrative(exec_summary="At some risk.", points=["Bulk migration slipped 3 days."])
    agent = StatusAgent(model="claude-test", client=_FakeClient(out))
    result = agent.run(_facts())

    assert result.exec_summary == "At some risk."
    assert result.points == ["Bulk migration slipped 3 days."]
    (call,) = agent._client.messages.calls
    assert call["output_format"] is StatusNarrative


def test_user_prompt_carries_the_facts_as_source_material():
    prompt = build_user_prompt(_facts())
    assert "HEALTH: yellow" in prompt
    assert "Bulk migration" in prompt  # the slipped task name is offered
    assert "+3" in prompt  # the launch shift
    assert "from these facts only" in prompt
