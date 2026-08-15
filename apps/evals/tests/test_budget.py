"""Cost ceilings, and the usage side channel that made them possible."""

from __future__ import annotations

from decimal import Decimal

from agents.usage import AgentUsage
from evals import budget


class _Usage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class _Response:
    def __init__(self, usage=None):
        self.usage = usage


# --- the side channel -------------------------------------------------------


def test_usage_is_read_off_a_response():
    used = AgentUsage.of(_Response(_Usage(721, 153)), "claude-sonnet-5")
    assert used == AgentUsage(model="claude-sonnet-5", input_tokens=721, output_tokens=153)


def test_a_response_without_usage_is_none_not_zero():
    """RC1-254 exists because a subject reported $0 for a run that spent 39
    seconds against a real model. Zeros would reintroduce exactly that lie —
    "this call was free" and "we did not measure it" must not look alike."""
    assert AgentUsage.of(_Response(), "claude-sonnet-5") is None
    assert AgentUsage.of(object(), "claude-sonnet-5") is None


def test_usage_carries_the_model():
    """Token counts price differently per model, and the drift digest already
    runs a different one from the planner."""
    assert AgentUsage.of(_Response(_Usage(1, 1)), "claude-haiku-4-5").model == "claude-haiku-4-5"


# --- ceilings ---------------------------------------------------------------


def test_a_run_within_its_ceiling_reports_no_breach():
    ceiling = budget.for_subject("status-narrative")
    assert ceiling.breaches(Decimal("0.057"), 40_000) == []


def test_cost_and_latency_breaches_are_reported_separately():
    """Different fixes: a slow run and an expensive one are not the same
    finding."""
    ceiling = budget.for_subject("status-narrative")
    found = ceiling.breaches(Decimal("0.42"), 130_000)

    assert len(found) == 2
    assert "cost" in found[0] and "180%" in found[0]
    assert "latency" in found[1]


def test_a_deterministic_subject_is_budgeted_at_zero():
    """A free subject that starts costing money has had a model introduced into
    it — a finding worth surfacing loudly, not a rounding difference."""
    ceiling = budget.for_subject("groundedness")
    assert ceiling.max_cost_usd == 0
    assert ceiling.breaches(Decimal("0.0001"), 100)


def test_every_billed_subject_has_a_ceiling():
    """An unbudgeted subject spends silently, which is what RC1-254 exists to
    stop."""
    from evals.subjects import BILLED

    for name in BILLED:
        assert budget.for_subject(name), f"{name} is billed and has no ceiling"


def test_each_ceiling_records_where_its_number_came_from():
    """A limit with no provenance is a number someone liked the look of."""
    for ceiling in budget.CEILINGS.values():
        assert ceiling.note, f"{ceiling.subject} has no note"
