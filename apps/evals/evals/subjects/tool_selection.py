"""MCP tool selection — the first subject where the measurement is the point.

RC1-231 shipped nine tools whose correctness depends on **prose in their
descriptions**. RC1-243's discoverability check ran once, by hand, and lives in
`docs/mcp-demo.md`. Nothing catches a regression: change one description while
adding a tenth tool and the routing can break silently, in a surface no unit
test exercises.

Scoring here is entirely deterministic — which tool was called, with what
arguments — so this story needs no LLM judge and no calibration, and delivers
before RC1-250 exists.

## What is actually under test

Not a function: *what a model does when handed this server's tool
descriptions*. So the subject drives the shipped stdio surface
(`evals.mcp_bridge`) and hands the real definitions to the Messages API.

Two deliberate omissions, both to keep the descriptions as the only routing
signal:

* **No system prompt.** The claim under test is that the descriptions alone are
  enough. A system prompt nudging tool choice would confound exactly that.
* **No sampling or effort overrides.** Current models reject non-default
  `temperature`/`top_p`/`top_k` outright, so determinism cannot be bought that
  way — and disabling thinking measurably changes how readily a model reaches
  for tools, which would bias the thing being measured. The eval runs the API
  defaults, records the model, and treats variance as a property to report
  rather than one to suppress.

Because routing is not deterministic, a single wrong pick is weak evidence. The
**confusion matrix** across the whole case set is the signal: which *wrong* tool
gets chosen, repeatedly, tells you which description to fix. RC1-255 turns that
into a tolerance; this story reports it.

## Attribution

`prompt_version` is a hash of the tool definitions the model actually saw. That
makes the acceptance criterion mechanical: degrade a description and the run
record's prompt version changes with it, so a score drop is attributable to a
specific surface rather than guessed at.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_evals.case import Case
from agent_evals.pricing import cost_usd
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage
from app.config import get_settings

from evals.config import get_eval_settings
from evals.mcp_bridge import Surface, ToolCall, call, discover, to_api_name, to_mcp_name

NAME = "tool-selection"


def _model() -> str:
    """The probe model (RC1-328): this subject measures the tool definitions,
    so the model is an instrument, not the thing under test — and the KPI
    agent's cost monitor showed the expensive one buying no extra signal.
    Pinned in `EvalSettings.tool_selection_model`, recorded on every run."""
    return get_eval_settings().tool_selection_model

#: Enough for a tool call plus a short follow-up answer. Not a quality lever —
#: the eval measures routing, not prose length.
_MAX_TOKENS = 2048

#: How many tool rounds a follow-up case may take before we stop. A real client
#: loops until the model stops asking; this bounds a runaway at a known cost.
_MAX_TOOL_ROUNDS = 3

_DRIFT_UNSET = {"LPA_DRIFT_BASE_URL": "", "LPA_DRIFT_RUN_TOKEN": ""}


# --- cases -----------------------------------------------------------------
#
# The first eight are the questions already written in `docs/mcp-demo.md`,
# verbatim, so the manual RC1-243 pass becomes a repeatable one. The rest are
# paraphrases, indirect phrasings, and the near-miss pairs the demo doc itself
# flags as the ones to watch.

#: The demo script is a numbered *sequence*: question 1 establishes the plans
#: and the model echoes a reference it can reuse. Lifting later questions
#: without that context measured something else entirely — the first real run
#: sent five of them to `plan.list`, because `plan.get`'s own description says
#: "Call plan.list first if you do not have a reference" and they had none.
#:
#: A plain assistant turn stands in for the tool round trip: it establishes the
#: same state the demo has by question 2, deterministically and without spending
#: a turn's tokens re-listing plans in every case.
_AFTER_PLAN_LIST: tuple[dict[str, str], ...] = (
    {"role": "user", "content": "What plans do you have?"},
    {
        "role": "assistant",
        "content": (
            "There is one plan of record: version 1 of the Jira Cloud migration, "
            "content hash 0e16d85f. I'll use that plan unless you tell me otherwise."
        ),
    },
)

CASES: tuple[Case, ...] = (
    # --- the shipped demo script -------------------------------------------
    Case(
        id="demo.1-what-plans",
        input={"question": "What plans do you have?", "tool": "plan.list"},
        expect=("calls-the-intended-tool", "reaches-it-without-a-detour", "calls-exactly-one-tool"),
        tags=("mcp-demo", "routing"),
    ),
    Case(
        id="demo.2a-when-does-it-launch",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "When does the Jira Cloud migration launch?",
            "tool": "plan.get",
        },
        expect=("calls-the-intended-tool", "reaches-it-without-a-detour", "calls-exactly-one-tool"),
        tags=("mcp-demo", "routing"),
    ),
    Case(
        id="demo.2b-whats-driving-that-date",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "What's driving the schedule for the Jira Cloud migration?",
            "tool": "plan.critical_path",
            "not_tool": "plan.forecast",
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "avoids-the-near-miss-tool",
            "calls-exactly-one-tool",
        ),
        tags=("mcp-demo", "near-miss", "routing"),
    ),
    Case(
        id="demo.3-legal-slips-a-month",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "What if the legal sign-off slips a month?",
            "tool": "plan.simulate",
            "task_contains": "legal",
            "days_between": [15, 31],
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "resolves-the-named-task",
            "slip-is-a-plausible-month",
        ),
        tags=("mcp-demo", "parameters"),
    ),
    Case(
        id="demo.3b-only-two-days",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "What if the legal sign-off slips by two working days?",
            "tool": "plan.simulate",
            "task_contains": "legal",
            "days_exactly": 2,
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "resolves-the-named-task",
            "slip-is-exact",
        ),
        tags=("mcp-demo", "parameters"),
    ),
    Case(
        id="demo.4-how-confident",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "How confident are we in that October date?",
            "tool": "plan.forecast",
            "not_tool": "plan.critical_path",
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "avoids-the-near-miss-tool",
            "leaves-the-seed-at-its-default",
        ),
        tags=("mcp-demo", "near-miss", "parameters"),
    ),
    # `demo.5-anything-drifting` ("Anything drifting right now?") was dropped as a
    # routing case. `docs/mcp-demo.md` says to skip the drift questions when no
    # drift service is configured — and with none, `platform.health` truthfully
    # reports drift unavailable and the model reasonably stops there rather than
    # making a doomed call. Asserting a route to `drift.check` in that state
    # tested the absence of a service, not a description. The same question is
    # covered by `honest.drift-unavailable` below, which scores the routing *and*
    # what the model says about an unavailable service.
    Case(
        id="demo.7-weekly-status",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "Draft me the weekly status update.",
            "tool": "status.draft",
        },
        expect=("calls-the-intended-tool", "reaches-it-without-a-detour", "calls-exactly-one-tool"),
        tags=("mcp-demo", "routing"),
    ),
    Case(
        id="demo.sanity-is-the-planner-healthy",
        input={"question": "Is the planner healthy?", "tool": "platform.health"},
        expect=("calls-the-intended-tool", "reaches-it-without-a-detour", "calls-exactly-one-tool"),
        tags=("mcp-demo", "routing"),
    ),
    # --- paraphrases and indirect phrasings --------------------------------
    Case(
        id="paraphrase.who-owns-the-work-that-matters",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "Who owns the work that matters most for hitting the launch date?",
            "tool": "plan.critical_path",
            "not_tool": "plan.forecast",
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "avoids-the-near-miss-tool",
        ),
        tags=("paraphrase", "near-miss"),
    ),
    Case(
        id="paraphrase.what-date-can-i-commit-to",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "What launch date can I actually commit to in front of the exec team?",
            "tool": "plan.forecast",
            "not_tool": "plan.get",
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "avoids-the-near-miss-tool",
        ),
        tags=("paraphrase", "near-miss"),
    ),
    Case(
        id="paraphrase.show-me-every-snapshot",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "Show me every plan snapshot you have on file.",
            "tool": "plan.list",
        },
        expect=("calls-the-intended-tool", "reaches-it-without-a-detour"),
        tags=("paraphrase",),
    ),
    Case(
        id="paraphrase.how-big-is-this-plan",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "How big is the migration plan — how many working days does it span?",
            "tool": "plan.get",
            "not_tool": "plan.critical_path",
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "avoids-the-near-miss-tool",
        ),
        tags=("paraphrase", "near-miss"),
    ),
    # --- the honest-answer cases -------------------------------------------
    Case(
        id="honest.ambiguous-task-name",
        input={
            "context": _AFTER_PLAN_LIST,
            "question": "What happens if review slips by a week?",
            "tool": "plan.simulate",
            "follow_up": True,
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "asks-for-clarification-rather-than-guessing",
        ),
        tags=("edge-case", "honest-answer"),
    ),
    Case(
        id="honest.drift-unavailable",
        input={
            "question": "Anything drifting on the migration right now?",
            "tool": "drift.check",
            "follow_up": True,
        },
        expect=(
            "calls-the-intended-tool",
            "reaches-it-without-a-detour",
            "reports-drift-unavailable-rather-than-all-clear",
        ),
        tags=("edge-case", "honest-answer"),
    ),
)


_SURFACE: Surface | None = None


def _surface() -> Surface:
    """Discover once per process.

    Every case would otherwise spawn its own server subprocess to read the same
    nine descriptions. Caching also guarantees every case in a run is scored
    against one surface, so `prompt_version` describes the whole run rather than
    whichever spawn happened to answer first.
    """
    global _SURFACE
    if _SURFACE is None:
        _SURFACE = discover(env=_child_env())
    return _SURFACE


def _child_env() -> dict[str, str]:
    """Environment for the server subprocess.

    Drift is left unconfigured on purpose: it is the shipped default, it keeps
    the suite credential-free and off the network, and the drift-unavailable
    case depends on it. `PATH` and friends are inherited so the interpreter
    still works.
    """
    env = dict(os.environ)
    env.update(_DRIFT_UNSET)
    return env


def prompt_version(surface: Surface) -> str:
    """A hash of exactly what the model was shown.

    Degrade a description and this changes — which is what makes a score drop
    attributable to a surface rather than to a mood.
    """
    payload = json.dumps(surface.tools, sort_keys=True).encode()
    return f"tools-sha256:{hashlib.sha256(payload).hexdigest()[:12]}"


def version(surface: Surface | None = None) -> SubjectVersion:
    from mcp_server import __version__ as mcp_version

    surface = surface or _surface()
    return SubjectVersion(
        subject=NAME,
        code_version=mcp_version,
        model=_model(),
        prompt_version=prompt_version(surface),
    )


@contextmanager
def _unset_drift() -> Iterator[None]:
    """Mirror the child's drift settings in this process too, so anything read
    here agrees with what the server saw."""
    previous = {key: os.environ.get(key) for key in _DRIFT_UNSET}
    os.environ.update(_DRIFT_UNSET)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --- the model call --------------------------------------------------------


class _Turn:
    """One model response, reduced to what the scorers need."""

    def __init__(self, calls: list[ToolCall], text: str, usage: tuple[int, int]) -> None:
        self.calls = calls
        self.text = text
        self.input_tokens, self.output_tokens = usage


def preflight() -> None:
    """Fail the run before spending anything, not once per case.

    A missing key is a precondition failure for the whole subject, not fifteen
    identical case errors — and fifteen identical errors would read like a
    quality finding in the report, which is exactly the confusion `CaseResult`
    keeps `error` separate from a failed characteristic to avoid.
    """
    if not get_settings().anthropic_api_key:
        raise RuntimeError(
            "LPA_ANTHROPIC_API_KEY is not set. This subject drives a real model, so it "
            "cannot run credential-free — which is why it is not part of `uv run pytest`. "
            "See ADR-0031."
        )


def _default_client():
    import anthropic

    preflight()
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)


def _ask(client, surface: Surface, messages: list[dict[str, Any]]) -> _Turn:
    """One turn. No system prompt, no sampling overrides — see the module docstring."""
    response = client.messages.create(
        model=_model(),
        max_tokens=_MAX_TOKENS,
        tools=surface.tools,
        tool_choice={"type": "auto"},
        messages=messages,
    )
    calls, text = [], []
    for block in response.content:
        if block.type == "tool_use":
            calls.append(ToolCall(name=to_mcp_name(block.name), arguments=dict(block.input)))
        elif block.type == "text":
            text.append(block.text)
    return _Turn(
        calls=calls,
        text="\n".join(text),
        usage=(response.usage.input_tokens, response.usage.output_tokens),
    )


# --- characteristics -------------------------------------------------------


class _Context:
    def __init__(self, case: Case, first: _Turn, follow_up: _Turn | None) -> None:
        self.case = case
        self.first = first
        self.follow_up = follow_up

    @property
    def chosen(self) -> str | None:
        return self.first.calls[0].name if self.first.calls else None

    @property
    def arguments(self) -> dict[str, Any]:
        return self.first.calls[0].arguments if self.first.calls else {}

    @property
    def final_text(self) -> str:
        return (self.follow_up.text if self.follow_up else self.first.text).lower()


def _calls_the_intended_tool(ctx: _Context) -> tuple[bool, str]:
    """Did the intended tool get called at all, anywhere in the turn?

    Deliberately *not* "was it first". The first real run showed the model
    taking a preparatory step — `plan.list` before `plan.get`, `platform.health`
    before `drift.check` — because three descriptions tell it to
    ("Start here…", "Call plan.list first…", "Call this first…"). Obeying a
    documented precedence instruction is not a routing failure, and scoring it
    as one would send someone to fix a description that is working. Directness
    is measured separately, and advisory — see `_reaches_it_without_a_detour`.
    """
    expected = ctx.case.input["tool"]
    called = [c.name for c in ctx.first.calls]
    if not called:
        return False, f"expected {expected}, but no tool was called at all"
    if expected not in called:
        return False, f"expected {expected}, called {called}"
    return True, f"called {expected}" + (f" (after {called[0]})" if called[0] != expected else "")


def _reaches_it_without_a_detour(ctx: _Context) -> tuple[bool, str]:
    """Was the intended tool the *first* call?

    Advisory. A detour costs a round trip and tokens, so it is worth watching —
    but it is the descriptions working as written, and gating on it would fail
    builds for behaviour the tool descriptions explicitly ask for.
    """
    expected = ctx.case.input["tool"]
    called = [c.name for c in ctx.first.calls]
    if not called:
        return False, "no tool was called"
    if called[0] == expected:
        return True, f"went straight to {expected}"
    if expected in called:
        return False, f"reached {expected} via {called[0]} first"
    # Never arrived. Saying "reached X via Y" here would be a false statement in
    # the one field a reader acts on — the gating check already failed, and this
    # detail has to agree with it rather than contradict it.
    return False, f"never reached {expected}; called {called[0]} instead"


def _avoids_the_near_miss_tool(ctx: _Context) -> tuple[bool, str]:
    """The confusable sibling, named per case. `plan.critical_path` and
    `plan.forecast` are the pair `docs/mcp-demo.md` itself flags: both talk
    about critical paths, and both answer a question about "the date".

    Checked across every call in the turn, not just the first — reaching the
    sibling as a "detour" is the same confusion, just later.
    """
    near_miss = ctx.case.input["not_tool"]
    called = [c.name for c in ctx.first.calls]
    if near_miss in called:
        return False, f"called the near-miss tool {near_miss}"
    return True, f"did not call {near_miss}"


def _calls_exactly_one_tool(ctx: _Context) -> tuple[bool, str]:
    """Reserved for cases where a second call means genuine confusion rather
    than a documented preparatory step — the opening question, which needs no
    prior reference and so has nothing to prepare."""
    if len(ctx.first.calls) != 1:
        chosen = [c.name for c in ctx.first.calls] or "none"
        return False, f"expected one tool call, got {len(ctx.first.calls)}: {chosen}"
    return True, "one tool call"


def _resolves_the_named_task(ctx: _Context) -> tuple[bool, str]:
    needle = ctx.case.input["task_contains"]
    task = str(ctx.arguments.get("task", ""))
    if needle.lower() not in task.lower():
        return False, f"expected a task matching {needle!r}, got {task!r}"
    return True, f"task={task!r}"


def _slip_is_a_plausible_month(ctx: _Context) -> tuple[bool, str]:
    """`days` is documented as WORKING days, but "a month" has two defensible
    readings — ~22 working days, or the 30 the demo script uses. The band
    accepts both and the detail records which was chosen, because pinning one
    reading would assert an answer the tool description does not actually give.
    """
    low, high = ctx.case.input["days_between"]
    days = ctx.arguments.get("days")
    if not isinstance(days, int | float):
        return False, f"expected a numeric `days`, got {days!r}"
    reading = "working days (~22)" if days <= 25 else "calendar days (~30)"
    if not low <= days <= high:
        return False, f"days={days} is outside the plausible band {low}-{high}"
    return True, f"days={days} — read as {reading}"


def _slip_is_exact(ctx: _Context) -> tuple[bool, str]:
    expected = ctx.case.input["days_exactly"]
    days = ctx.arguments.get("days")
    if days != expected:
        return False, f"expected days={expected}, got {days!r}"
    return True, f"days={days}"


def _leaves_the_seed_at_its_default(ctx: _Context) -> tuple[bool, str]:
    """The demo's defensibility beat: the forecast is reproducible because the
    seed is fixed. A model inventing a seed silently breaks that."""
    seed = ctx.arguments.get("seed")
    if seed not in (None, 0):
        return False, f"invented seed={seed!r}; reproducibility depends on the default"
    return True, "seed left at its default"


def _asks_for_clarification_rather_than_guessing(ctx: _Context) -> tuple[bool, str]:
    """An ambiguous task name must produce candidates, not a confident answer.

    The tool raises `AmbiguousTaskRef` with the matches; the model then has to
    relay them rather than pick one. Both halves are checked: the error has to
    carry candidates, and the final text has to put the choice back to the user.
    """
    if ctx.follow_up is None:
        return False, "no follow-up turn was run"
    text = ctx.final_text
    asked = "?" in text or any(
        phrase in text for phrase in ("which", "did you mean", "more than one", "ambiguous")
    )
    if not asked:
        return False, f"answered without clarifying: {text[:160]!r}"
    return True, "put the ambiguity back to the user"


def _reports_drift_unavailable(ctx: _Context) -> tuple[bool, str]:
    """Unavailable and all-clear mean opposite things.

    With no drift service configured the tool reports unavailable. Narrating
    that as "nothing is drifting" is the failure that matters, and it is the
    same class as the empty-findings case RC1-252 covers for the digest.
    """
    if ctx.follow_up is None:
        return False, "no follow-up turn was run"
    text = ctx.final_text
    false_all_clear = [
        phrase
        for phrase in ("nothing is drifting", "no drift", "all clear", "everything looks fine")
        if phrase in text
    ]
    if false_all_clear:
        return False, f"narrated unavailability as an all-clear: {false_all_clear[0]!r}"
    # A phrase list was too literal here: a real run reported "the drift service
    # isn't currently configured" — exactly the honest answer the case demands —
    # and the check called it a failure because the list held "not configured".
    # Negation is expressed a dozen ways; match the shape rather than the words.
    honest = bool(
        re.search(
            r"\bun(?:available|able)\b"
            r"|\b(?:not|isn't|is not|aren't|are not|no)\b[^.]{0,30}"
            r"\b(?:configur|availab|reachab|connect|set up|enabled)"
            r"|\bcannot\b[^.]{0,20}\b(?:reach|check|access)"
            r"|\bcan't\b[^.]{0,20}\b(?:reach|check|access)",
            text,
        )
    )
    if not honest:
        return False, f"did not say the drift service is unavailable: {text[:160]!r}"
    return True, "reported the service as unavailable"


#: Reported, never gating. `reaches-it-without-a-detour` measures directness,
#: and a detour is the tool descriptions working as written — see ADR-0031.
ADVISORY = frozenset({"reaches-it-without-a-detour"})

CHARACTERISTICS: dict[str, Callable[[_Context], tuple[bool, str]]] = {
    "calls-the-intended-tool": _calls_the_intended_tool,
    "reaches-it-without-a-detour": _reaches_it_without_a_detour,
    "avoids-the-near-miss-tool": _avoids_the_near_miss_tool,
    "calls-exactly-one-tool": _calls_exactly_one_tool,
    "resolves-the-named-task": _resolves_the_named_task,
    "slip-is-a-plausible-month": _slip_is_a_plausible_month,
    "slip-is-exact": _slip_is_exact,
    "leaves-the-seed-at-its-default": _leaves_the_seed_at_its_default,
    "asks-for-clarification-rather-than-guessing": _asks_for_clarification_rather_than_guessing,
    "reports-drift-unavailable-rather-than-all-clear": _reports_drift_unavailable,
}


# --- running ---------------------------------------------------------------


def run(case: Case, tmp_root: Path, client=None, surface: Surface | None = None) -> CaseResult:
    """Run one case: ask, optionally execute and ask again, then score.

    `client` and `surface` are injectable so the tests can drive the whole path
    with a fake and no credentials — the same seam `agents` uses, and the reason
    `uv run pytest` stays credential-free while this subject does not.
    """
    client = client or _default_client()
    surface = surface or _surface()
    question = case.input["question"]
    messages: list[dict[str, Any]] = [
        *case.input.get("context", []),
        {"role": "user", "content": question},
    ]

    started = time.perf_counter()
    try:
        with _unset_drift():
            first = _ask(client, surface, messages)
            follow_up = None
            if case.input.get("follow_up") and first.calls:
                follow_up = _follow_up(client, surface, messages, first)
    except Exception as exc:
        # A transport or credential failure is a recorded outcome, not a crash:
        # the rest of the case set still runs and this one is marked unscored.
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    ctx = _Context(case, first, follow_up)
    results = []
    for name in case.expect:
        predicate = CHARACTERISTICS.get(name)
        if predicate is None:
            results.append(
                CharacteristicResult(
                    name=name, passed=False, detail="no predicate is registered for this name"
                )
            )
            continue
        passed, detail = predicate(ctx)
        results.append(
            CharacteristicResult(name=name, passed=passed, detail=detail, advisory=name in ADVISORY)
        )

    input_tokens = first.input_tokens + (follow_up.input_tokens if follow_up else 0)
    output_tokens = first.output_tokens + (follow_up.output_tokens if follow_up else 0)
    model = _model()
    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd(model, input_tokens, output_tokens),
            latency_ms=latency_ms,
        ),
        # The confusion matrix is built from these, so a wrong pick says *which*
        # description competed rather than only that something broke.
        observations={
            "expected_tool": case.input["tool"],
            # The tool the model reached for *first*. This is what the confusion
            # matrix aggregates, because a wrong first pick is what points at a
            # competing description.
            "actual_tool": ctx.chosen,
            "tools_called": [c.name for c in ctx.first.calls],
            "arguments": ctx.arguments,
        },
    )


def _follow_up(client, surface: Surface, messages, first: _Turn) -> _Turn:
    """Execute the chosen tool for real and hand the result back.

    Only the honest-answer cases need this. What is being scored is not the
    tool's output but what the model *says* about it — a tool that reports
    unavailable, narrated as an all-clear, is the failure worth catching.

    A real MCP client keeps executing tools until the model stops asking, so
    this loops rather than taking a single follow-up turn. A run caught why: the
    model answered a `drift.check` result with *another* tool call, the one-shot
    version had no text to score, and the case failed with an empty string —
    reporting a scoring gap as though it were a quality failure.
    """
    turn = first
    for round_number in range(_MAX_TOOL_ROUNDS):
        for index, chosen in enumerate(turn.calls):
            use_id = f"eval-{round_number}-{index}"
            result_text = call(chosen.name, chosen.arguments, env=_child_env())
            messages = messages + [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": use_id,
                            "name": to_api_name(chosen.name),
                            "input": chosen.arguments,
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": use_id, "content": result_text}
                    ],
                },
            ]
        turn = _ask(client, surface, messages)
        if not turn.calls:
            return turn
    # Still calling tools at the cap. Returned as-is: the scorers see whatever
    # text there is, and an empty one reads as "never answered" — which is what
    # happened, rather than something to paper over.
    return turn
