"""The two billed paths, driven with a fake client and no credentials.

Both go through `messages.parse` exactly as `agents` does, so the seam is the
injectable client — which is what keeps `uv run pytest` credential-free while
the commands themselves cost money (ADR-0031, ADR-0033).
"""

from __future__ import annotations

import pytest
from evals import judge, seedgen
from evals.rubric import DIMENSION_KEYS, RUBRIC_VERSION, Score
from evals.seeds import Seed


class _Parsed:
    def __init__(self, parsed_output, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason


class FakeClient:
    """Returns scripted `messages.parse` results and records the requests."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    def parse(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0) if self._responses else self._responses[-1]


def _scored(**overrides):
    values = {key: 2 for key in DIMENSION_KEYS}
    values.update(overrides)
    return judge._Scored(
        **{f"{key}_reason": f"because of {key}" for key in DIMENSION_KEYS},
        **values,
    )


def _seed(seed_id="status-narrative-00-agent"):
    return Seed(
        id=seed_id,
        subject="status-narrative",
        variant="agent",
        facts={"period_label": "Week 1", "health": "red", "launch_shift_days": 24},
        output={"exec_summary": "Red: launch slipped 24 days.", "points": ["Legal slipped."]},
        generator_version="test",
    )


# --- the judge --------------------------------------------------------------


def test_the_judge_scores_every_dimension_and_records_its_version():
    client = FakeClient(_Parsed(_scored(groundedness=1, tone=0)))
    label = judge.score(_seed(), client=client, model="claude-sonnet-5")

    assert set(label.scores) == set(DIMENSION_KEYS)
    assert label.scores["groundedness"] == Score.PARTIAL
    assert label.scores["tone"] == Score.FAILS
    assert label.scorer == judge.JUDGE_VERSION, "not 'judge' — two prompt versions must not merge"
    assert label.rubric_version == RUBRIC_VERSION


def test_the_judge_reads_the_same_rubric_the_human_does():
    """A judge scoring against different wording is not a calibration, it is two
    unrelated measurements."""
    from evals.rubric import rubric_text

    client = FakeClient(_Parsed(_scored()))
    judge.score(_seed(), client=client, model="claude-sonnet-5")

    assert rubric_text() in client.requests[0]["system"]


def test_the_judge_is_shown_the_facts_and_not_how_the_output_was_made():
    """Groundedness needs the facts. The variant and the generator would leak
    the answer — the judge must not know an output came from the degraded
    prompt."""
    client = FakeClient(_Parsed(_scored()))
    judge.score(_seed(), client=client, model="claude-sonnet-5")

    prompt = client.requests[0]["messages"][0]["content"]
    assert "launch_shift_days" in prompt
    assert "Legal slipped." in prompt
    assert "degraded" not in prompt and "variant" not in prompt


def test_an_unscoreable_response_raises_rather_than_becoming_a_zero():
    """ "The judge could not answer" and "the judge said this is bad" are
    different findings. A real run hit this: at max_tokens=1024 the JSON
    truncated mid-string and the sixth seed died."""
    client = FakeClient(_Parsed(None, stop_reason="max_tokens"))
    with pytest.raises(judge.JudgeRefused, match="max_tokens"):
        judge.score(_seed(), client=client, model="claude-sonnet-5")


def test_the_token_budget_leaves_room_for_thinking():
    """max_tokens caps thinking and output together, and adaptive thinking is on
    by default on current models — the reason 1024 was not enough."""
    client = FakeClient(_Parsed(_scored()))
    judge.score(_seed(), client=client, model="claude-sonnet-5")
    assert client.requests[0]["max_tokens"] >= 4096


# --- seed generation --------------------------------------------------------


def test_generation_produces_three_variants_per_fact_set():
    """A set of uniformly good outputs cannot calibrate anything — both scorers
    say 2, kappa is undefined, and the honest reading is "no variance"."""
    from planner_core import StatusNarrative

    narrative = StatusNarrative(exec_summary="Something happened.", points=["A point."])
    client = FakeClient(*[_Parsed(narrative) for _ in range(len(seedgen.FACT_SETS) * 2)])

    seeds = seedgen.generate(client=client, model="claude-sonnet-5")

    assert len(seeds) == len(seedgen.FACT_SETS) * 3
    assert {seed.variant for seed in seeds} == {"agent", "fallback", "degraded"}
    assert len({seed.id for seed in seeds}) == len(seeds), "ids must be unique"


def test_the_degraded_variant_uses_a_different_prompt_than_the_agent():
    """If both variants used the shipped prompt, the pair would be noise rather
    than a quality spread."""
    from agents.status import SYSTEM_PROMPT
    from planner_core import StatusNarrative

    narrative = StatusNarrative(exec_summary="x", points=[])
    client = FakeClient(*[_Parsed(narrative) for _ in range(len(seedgen.FACT_SETS) * 2)])
    seedgen.generate(client=client, model="claude-sonnet-5")

    systems = [request["system"] for request in client.requests]
    assert SYSTEM_PROMPT in systems
    assert seedgen.DEGRADED_SYSTEM_PROMPT in systems


def test_every_seed_carries_the_facts_it_was_written_from():
    """Groundedness is unscoreable otherwise — by a human or by the judge."""
    from planner_core import StatusNarrative

    narrative = StatusNarrative(exec_summary="x", points=[])
    client = FakeClient(*[_Parsed(narrative) for _ in range(len(seedgen.FACT_SETS) * 2)])

    for seed in seedgen.generate(client=client, model="claude-sonnet-5"):
        assert seed.facts.get("period_label")
        assert "health" in seed.facts


def test_the_fallback_variant_costs_nothing():
    """It is rule-written, so it anchors the accurate-but-flat corner for free."""
    from planner_core import StatusNarrative

    narrative = StatusNarrative(exec_summary="x", points=[])
    client = FakeClient(*[_Parsed(narrative) for _ in range(len(seedgen.FACT_SETS) * 2)])
    seedgen.generate(client=client, model="claude-sonnet-5")

    # Two billed calls per fact set: the agent and the degraded variant.
    assert len(client.requests) == len(seedgen.FACT_SETS) * 2
