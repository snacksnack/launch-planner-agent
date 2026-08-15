"""Tool-selection scoring, driven with a fake model and no credentials.

The subject itself needs an API key and spends tokens — that is why it is not
part of `uv run pytest` (ADR-0031). What *is* tested here is everything around
the model: the name translation the Messages API forces, the scorers, the
confusion-matrix observations, and the honest-answer cases. The seam is the
injectable client, the same one `agents` uses.

The stdio surface is real in `test_mcp_bridge.py` and faked here, so these tests
stay fast and do not spawn a subprocess per case.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from agent_evals.case import Case
from app.config import get_settings
from evals.mcp_bridge import Surface, to_api_name, to_mcp_name
from evals.subjects import tool_selection


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for key, value in kw.items():
            setattr(self, key, value)


class _Usage:
    def __init__(self, input_tokens=1000, output_tokens=50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage or _Usage()


class FakeClient:
    """Returns scripted turns. `messages.create` is the only surface used."""

    def __init__(self, *turns):
        self._turns = list(turns)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._turns.pop(0) if self._turns else _Response([_Block("text", text="")])


def tool_use(mcp_name: str, **arguments):
    """A tool_use block as the API returns it — in API names, with `__`."""
    return _Block("tool_use", name=to_api_name(mcp_name), input=arguments, id="t1")


def text(value: str):
    return _Block("text", text=value)


@pytest.fixture
def surface():
    return Surface(
        tools=[
            {"name": to_api_name(name), "description": f"desc for {name}", "input_schema": {}}
            for name in (
                "plan.list",
                "plan.get",
                "plan.critical_path",
                "plan.simulate",
                "plan.forecast",
                "drift.check",
                "status.draft",
                "platform.health",
            )
        ],
        mcp_names=("plan.list",),
    )


def _run(case, client, surface, tmp_path):
    return tool_selection.run(case, tmp_path, client=client, surface=surface)


# --- the name translation the API forces ------------------------------------


def test_api_names_round_trip():
    """`plan.list` cannot be sent to the Messages API — dots are outside the
    permitted tool-name charset — so the mapping has to be reversible or the
    report would name a tool nobody shipped."""
    assert to_api_name("plan.critical_path") == "plan__critical_path"
    assert to_mcp_name("plan__critical_path") == "plan.critical_path"


def test_an_unknown_tool_name_survives_into_the_report():
    """A hallucinated name must not be mangled into something plausible."""
    assert to_mcp_name("totally_made_up") == "totally_made_up"


# --- routing ----------------------------------------------------------------


def test_correct_route_passes_and_is_recorded(surface, tmp_path):
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.list"},
        expect=("calls-the-intended-tool",),
    )
    result = _run(case, FakeClient(_Response([tool_use("plan.list")])), surface, tmp_path)

    assert result.passed
    assert result.observations["expected_tool"] == "plan.list"
    assert result.observations["actual_tool"] == "plan.list"


def test_a_mis_route_records_which_tool_competed(surface, tmp_path):
    """The confusion matrix's whole value: *which* wrong tool, not just that
    something broke."""
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.critical_path"},
        expect=("calls-the-intended-tool",),
    )
    result = _run(case, FakeClient(_Response([tool_use("plan.forecast")])), surface, tmp_path)

    assert not result.passed
    assert result.observations["actual_tool"] == "plan.forecast"
    assert "called ['plan.forecast']" in result.characteristics[0].detail


def test_answering_with_no_tool_at_all_fails(surface, tmp_path):
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.list"},
        expect=("calls-the-intended-tool",),
    )
    result = _run(
        case, FakeClient(_Response([text("Sure, you have three plans.")])), surface, tmp_path
    )

    assert not result.passed
    assert "no tool was called" in result.characteristics[0].detail
    assert result.observations["actual_tool"] is None


def test_the_near_miss_check_is_separate_from_routing(surface, tmp_path):
    """Two findings, not one: choosing the sibling is a description problem;
    choosing something unrelated is a different problem."""
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.critical_path", "not_tool": "plan.forecast"},
        expect=("calls-the-intended-tool", "avoids-the-near-miss-tool"),
    )
    result = _run(case, FakeClient(_Response([tool_use("plan.forecast")])), surface, tmp_path)

    routing, near_miss = result.characteristics
    assert not routing.passed
    assert not near_miss.passed
    assert "near-miss" in near_miss.detail


def test_multiple_tool_calls_fail_the_exactly_one_check(surface, tmp_path):
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.list"},
        expect=("calls-exactly-one-tool",),
    )
    client = FakeClient(_Response([tool_use("plan.list"), tool_use("plan.get")]))
    result = _run(case, client, surface, tmp_path)
    assert not result.passed
    assert "got 2" in result.characteristics[0].detail


# --- parameters -------------------------------------------------------------


def test_task_resolution_is_scored_on_the_argument(surface, tmp_path):
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.simulate", "task_contains": "legal"},
        expect=("resolves-the-named-task",),
    )
    ok = _run(
        case,
        FakeClient(_Response([tool_use("plan.simulate", task="legal review", days=20)])),
        surface,
        tmp_path,
    )
    bad = _run(
        case,
        FakeClient(_Response([tool_use("plan.simulate", task="vendor contract", days=20)])),
        surface,
        tmp_path,
    )
    assert ok.passed and not bad.passed
    assert "vendor contract" in bad.characteristics[0].detail


@pytest.mark.parametrize(
    "days,expected_reading",
    [(22, "working days"), (30, "calendar days")],
)
def test_a_month_accepts_both_readings_and_records_which(surface, tmp_path, days, expected_reading):
    """`days` is documented as working days but the demo script uses 30. Pinning
    one reading would assert an answer the description doesn't give — so the band
    accepts both and the detail says which was chosen."""
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.simulate", "days_between": [15, 31]},
        expect=("slip-is-a-plausible-month",),
    )
    result = _run(
        case,
        FakeClient(_Response([tool_use("plan.simulate", task="legal", days=days)])),
        surface,
        tmp_path,
    )
    assert result.passed
    assert expected_reading in result.characteristics[0].detail


def test_an_implausible_slip_fails(surface, tmp_path):
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.simulate", "days_between": [15, 31]},
        expect=("slip-is-a-plausible-month",),
    )
    result = _run(
        case,
        FakeClient(_Response([tool_use("plan.simulate", task="legal", days=180)])),
        surface,
        tmp_path,
    )
    assert not result.passed
    assert "outside the plausible band" in result.characteristics[0].detail


def test_an_invented_seed_fails_reproducibility(surface, tmp_path):
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.forecast"},
        expect=("leaves-the-seed-at-its-default",),
    )
    invented = _run(
        case, FakeClient(_Response([tool_use("plan.forecast", seed=42)])), surface, tmp_path
    )
    default = _run(case, FakeClient(_Response([tool_use("plan.forecast")])), surface, tmp_path)
    assert not invented.passed
    assert default.passed


# --- the honest-answer cases ------------------------------------------------


def test_a_clarifying_question_passes_and_a_confident_guess_fails(surface, tmp_path, monkeypatch):
    monkeypatch.setattr(tool_selection, "call", lambda *a, **kw: "[ambiguous_task] matches: A, B")
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.simulate", "follow_up": True},
        expect=("asks-for-clarification-rather-than-guessing",),
    )
    asked = _run(
        case,
        FakeClient(
            _Response([tool_use("plan.simulate", task="review", days=5)]),
            _Response([text("Two tasks match 'review' — did you mean the legal one?")]),
        ),
        surface,
        tmp_path,
    )
    guessed = _run(
        case,
        FakeClient(
            _Response([tool_use("plan.simulate", task="review", days=5)]),
            _Response([text("The launch moves to November 13.")]),
        ),
        surface,
        tmp_path,
    )
    assert asked.passed
    assert not guessed.passed
    assert "answered without clarifying" in guessed.characteristics[0].detail


def test_unavailable_drift_narrated_as_an_all_clear_is_caught(surface, tmp_path, monkeypatch):
    """The failure that matters: 'unavailable' and 'nothing is drifting' mean
    opposite things, and only one of them is true."""
    monkeypatch.setattr(
        tool_selection, "call", lambda *a, **kw: "[drift_unavailable] not configured"
    )
    case = Case(
        id="c",
        input={"question": "q", "tool": "drift.check", "follow_up": True},
        expect=("reports-drift-unavailable-rather-than-all-clear",),
    )
    honest = _run(
        case,
        FakeClient(
            _Response([tool_use("drift.check")]),
            _Response([text("The drift service is not configured, so I can't tell you.")]),
        ),
        surface,
        tmp_path,
    )
    false_all_clear = _run(
        case,
        FakeClient(
            _Response([tool_use("drift.check")]),
            _Response([text("Good news — nothing is drifting right now.")]),
        ),
        surface,
        tmp_path,
    )
    assert honest.passed
    assert not false_all_clear.passed
    assert "all-clear" in false_all_clear.characteristics[0].detail


# --- request shape and accounting -------------------------------------------


def test_the_request_carries_no_system_prompt_and_no_sampling_overrides(surface, tmp_path):
    """Both omissions are deliberate. A system prompt would confound the claim
    that the descriptions alone route correctly; sampling parameters are
    rejected outright by current models, so determinism cannot be bought there.
    """
    client = FakeClient(_Response([tool_use("plan.list")]))
    case = Case(
        id="c", input={"question": "q", "tool": "plan.list"}, expect=("calls-exactly-one-tool",)
    )
    _run(case, client, surface, tmp_path)

    request = client.requests[0]
    assert "system" not in request
    assert not {"temperature", "top_p", "top_k"} & request.keys()
    assert request["tool_choice"] == {"type": "auto"}
    assert request["tools"] == surface.tools


def test_usage_carries_a_real_cost(surface, tmp_path, monkeypatch):
    """Tokens are the measurement; cost is the thing RC1-254 budgets against."""
    monkeypatch.setenv("LPA_ANTHROPIC_MODEL", "claude-sonnet-5")
    get_settings.cache_clear()
    try:
        case = Case(
            id="c", input={"question": "q", "tool": "plan.list"}, expect=("calls-exactly-one-tool",)
        )
        client = FakeClient(_Response([tool_use("plan.list")], usage=_Usage(1_000_000, 100_000)))
        result = _run(case, client, surface, tmp_path)
        # 1M input at $3.00/M + 100k output at $15.00/M = $3.00 + $1.50
        assert result.usage.cost_usd == Decimal("4.50")
        assert result.usage.input_tokens == 1_000_000
    finally:
        get_settings.cache_clear()


def test_a_transport_failure_is_recorded_not_raised(surface, tmp_path):
    class Boom:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("connection reset")

    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.list"},
        expect=("calls-the-intended-tool",),
    )
    result = _run(case, Boom(), surface, tmp_path)
    assert result.error == "RuntimeError: connection reset"
    assert result.passed is False


def test_prompt_version_changes_when_a_description_changes(surface):
    """The attribution hook the acceptance criterion leans on."""
    before = tool_selection.prompt_version(surface)
    degraded = Surface(
        tools=[{**surface.tools[0], "description": "does stuff"}, *surface.tools[1:]],
        mcp_names=surface.mcp_names,
    )
    assert tool_selection.prompt_version(degraded) != before


# --- the detour split (RC1-249, after the first real run) --------------------


def test_a_preparatory_call_passes_gating_but_fails_the_advisory_check(surface, tmp_path):
    """The finding that motivated the split: three descriptions tell the model
    to call something else first ("Start here…", "Call plan.list first…",
    "Call this first…"). Obeying them is not a routing failure — but it costs a
    round trip, so it is reported rather than ignored."""
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.get"},
        expect=("calls-the-intended-tool", "reaches-it-without-a-detour"),
    )
    client = FakeClient(_Response([tool_use("plan.list"), tool_use("plan.get")]))
    result = _run(case, client, surface, tmp_path)

    gating, advisory = result.characteristics
    assert result.passed, "a documented preparatory step must not fail the case"
    assert gating.passed and not gating.advisory
    assert not advisory.passed and advisory.advisory
    assert "via plan.list first" in advisory.detail
    assert result.observations["tools_called"] == ["plan.list", "plan.get"]


def test_never_calling_the_intended_tool_still_fails(surface, tmp_path):
    """The split must not become a loophole — a detour that never arrives is
    still a miss."""
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.get"},
        expect=("calls-the-intended-tool", "reaches-it-without-a-detour"),
    )
    result = _run(case, FakeClient(_Response([tool_use("plan.list")])), surface, tmp_path)
    assert not result.passed


def test_the_near_miss_check_sees_past_the_first_call(surface, tmp_path):
    """Reaching the confusable sibling as a second call is the same confusion,
    just later — so it is checked across the whole turn."""
    case = Case(
        id="c",
        input={"question": "q", "tool": "plan.critical_path", "not_tool": "plan.forecast"},
        expect=("avoids-the-near-miss-tool",),
    )
    client = FakeClient(_Response([tool_use("plan.critical_path"), tool_use("plan.forecast")]))
    assert not _run(case, client, surface, tmp_path).passed


def test_case_context_is_prepended_to_the_conversation(surface, tmp_path):
    """The demo script is a sequence; question 2 runs with question 1's answer
    already in the conversation. Without that, later questions were measuring
    cold-start behaviour instead of the descriptions."""
    context = (
        {"role": "user", "content": "What plans do you have?"},
        {"role": "assistant", "content": "Version 1, hash 0e16d85f."},
    )
    case = Case(
        id="c",
        input={"context": context, "question": "When does it launch?", "tool": "plan.get"},
        expect=("calls-the-intended-tool",),
    )
    client = FakeClient(_Response([tool_use("plan.get")]))
    _run(case, client, surface, tmp_path)

    sent = client.requests[0]["messages"]
    assert len(sent) == 3
    assert sent[0]["content"] == "What plans do you have?"
    assert sent[-1]["content"] == "When does it launch?"


def test_every_shipped_case_names_only_registered_characteristics():
    """A typo in a case file would otherwise fail at $0.02 a case, mid-run."""
    for case in tool_selection.CASES:
        unknown = set(case.expect) - set(tool_selection.CHARACTERISTICS)
        assert not unknown, f"{case.id} names unregistered characteristic(s): {unknown}"


def test_the_advisory_detail_never_claims_an_unreached_tool(surface, tmp_path):
    """Caught by a real run: the detail said "reached drift.check via
    platform.health" for a turn that never called drift.check. `detail` is the
    field a reader acts on — it must agree with the gating result."""
    case = Case(
        id="c",
        input={"question": "q", "tool": "drift.check"},
        expect=("calls-the-intended-tool", "reaches-it-without-a-detour"),
    )
    result = _run(case, FakeClient(_Response([tool_use("platform.health")])), surface, tmp_path)

    gating, advisory = result.characteristics
    assert not gating.passed and not advisory.passed
    assert "never reached drift.check" in advisory.detail
    assert "reached drift.check via" not in advisory.detail


def test_the_follow_up_loops_until_the_model_stops_calling_tools(surface, tmp_path, monkeypatch):
    """A real run caught this: the model answered a tool result with another
    tool call, the one-shot follow-up had no text, and the case failed with an
    empty string — a scoring gap reported as a quality failure."""
    monkeypatch.setattr(
        tool_selection, "call", lambda *a, **kw: "[drift_unavailable] not configured"
    )
    case = Case(
        id="c",
        input={"question": "q", "tool": "drift.check", "follow_up": True},
        expect=("reports-drift-unavailable-rather-than-all-clear",),
    )
    client = FakeClient(
        _Response([tool_use("platform.health")]),  # first turn
        _Response([tool_use("drift.check")]),  # answers the result with another call
        _Response([text("The drift service is not configured.")]),  # finally, prose
    )
    result = _run(case, client, surface, tmp_path)

    assert result.passed
    assert len(client.requests) == 3, "should have kept going until the model stopped"


def test_the_follow_up_stops_at_the_round_cap(surface, tmp_path, monkeypatch):
    """A model that never stops calling tools must not loop forever — and the
    empty text it leaves behind should read as 'never answered', not be papered
    over."""
    monkeypatch.setattr(tool_selection, "call", lambda *a, **kw: "result")
    case = Case(
        id="c",
        input={"question": "q", "tool": "drift.check", "follow_up": True},
        expect=("reports-drift-unavailable-rather-than-all-clear",),
    )
    client = FakeClient(*[_Response([tool_use("drift.check")]) for _ in range(10)])
    result = _run(case, client, surface, tmp_path)

    assert not result.passed
    assert len(client.requests) == tool_selection._MAX_TOOL_ROUNDS + 1
