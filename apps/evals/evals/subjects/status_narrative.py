"""Status-update goldens — must-say and must-not-say, on fresh generation.

RC1-251's `groundedness` subject scores a frozen corpus and answers "did the
checker change". This one generates output now and answers "did the *agent*
change", which is the question a regression suite is actually for.

## Two subjects, not one flag

`status-narrative` runs the shipped `StatusAgent`; `status-narrative-fallback`
runs `planner_core.fallback_narrative`. They are registered separately because
RC1-252 requires the two scored "as separate subject versions" — and the run
record is what makes that real: one carries a model and a prompt hash, the other
records `model: None` and costs nothing. Same cases, same characteristics, two
comparable rows in the run log.

That comparison is the point. The deterministic narrative is perfectly grounded
and complete by construction and reads like a machine; the LLM one is better
prose with a real hallucination risk. Scoring them against one rubric says how
much the prose is worth and what it costs.

## What is checked

* **must-say** — the facts a reader needs: the health state, the launch movement,
  and the high-significance changes (breaches, newly critical work). Derived from
  the facts rather than hand-written per case, so a new fact set cannot silently
  ship with no expectations.
* **must-not-say** — no unsupported claims, and no health state the facts
  contradict. Both come from `evals.groundedness`, which RC1-251 validated to
  zero false positives on the committed corpus.
* **the two edges** — a week with nothing to report must not manufacture
  activity, and a period with no baseline must say there is nothing to compare
  rather than narrating silence as "no changes". Those mean opposite things, and
  conflating them is the exact failure the drift digest's empty-findings case
  guards against on the other side.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from agents.status import DEFAULT_MODEL, SYSTEM_PROMPT, StatusAgent
from app.config import get_settings
from planner_core import StatusFacts, fallback_narrative

from evals import groundedness
from evals.case import Case
from evals.pricing import cost_usd
from evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage
from evals.seedgen import FACT_SETS

NAME = "status-narrative"
FALLBACK_NAME = "status-narrative-fallback"

_MAX_TOKENS = 2000

#: Phrases that narrate an absent baseline as though nothing changed. "Nothing to
#: compare against" and "nothing changed" mean opposite things, and only one of
#: them is true when no baseline exists.
_NO_BASELINE_FORBIDDEN = (
    "no changes",
    "nothing changed",
    "unchanged from last week",
    "no movement since",
    "holds steady against the baseline",
)

#: What a genuinely quiet week must not claim to have done.
_QUIET_WEEK_FORBIDDEN = ("slipped", "newly critical", "breach", "missed deadline")


def _must_say(facts: StatusFacts) -> list[Any]:
    """The facts a reader needs, derived rather than hand-listed.

    Derived so a fact set added later cannot ship with an empty expectation
    list and score a silent pass — the failure mode of every hand-maintained
    golden.
    """
    required: list[Any] = []
    if facts.launch_after:
        required.append(facts.launch_after.isoformat())
    if facts.launch_shift_days:
        required.append(abs(facts.launch_shift_days))
    # High-significance changes only. Omitting a minor detail is not a miss;
    # omitting a breach or newly critical work is.
    required += [b.constraint_id for b in facts.breaches]
    required += [c.name for c in facts.newly_critical]
    required += [c.name for c in facts.slipped[:2]]
    return required


def _case(index: int, facts: StatusFacts) -> Case:
    expect = ["states-the-required-facts", "no-unsupported-claims"]
    if facts.baseline_version is None:
        expect.append("says-there-is-nothing-to-compare")
    elif not any(
        (
            facts.slipped,
            facts.newly_critical,
            facts.breaches,
            facts.milestone_drift,
            facts.raid_added,
            facts.launch_shift_days,
        )
    ):
        expect.append("does-not-manufacture-activity")
    return Case(
        id=f"status-{index:02d}-{facts.period_label.replace(' ', '-').lower()}",
        input={"fact_index": index},
        expect=tuple(expect),
        tags=("status", facts.health.value),
    )


CASES: tuple[Case, ...] = tuple(_case(i, f) for i, f in enumerate(FACT_SETS))


def preflight() -> None:
    if not get_settings().anthropic_api_key:
        raise RuntimeError(
            "LPA_ANTHROPIC_API_KEY is not set. This subject generates fresh narratives. "
            "`status-narrative-fallback` is the deterministic twin and needs no credentials."
        )


def prompt_version() -> str:
    """Hash of the system prompt the narrative was written from — the same
    attribution hook RC1-249 uses for tool descriptions, so a prompt edit shows
    up in the run record beside the score it moved."""
    return f"status-sha256:{hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]}"


def version() -> SubjectVersion:
    from mcp_server import __version__ as code_version

    return SubjectVersion(
        subject=NAME,
        code_version=code_version,
        model=get_settings().anthropic_model or DEFAULT_MODEL,
        prompt_version=prompt_version(),
    )


def fallback_version() -> SubjectVersion:
    from mcp_server import __version__ as code_version

    return SubjectVersion(
        subject=FALLBACK_NAME,
        code_version=code_version,
        # Rule-written. Stated rather than omitted, so the two rows in the run
        # log are obviously the same cases scored on different producers.
        model=None,
        prompt_version=None,
    )


def _score(case: Case, facts: StatusFacts, text: str, usage: Usage) -> CaseResult:
    required = _must_say(facts)
    omissions = groundedness.missing(text, required)
    report = groundedness.check(text, facts.model_dump(mode="json"))

    results = [
        CharacteristicResult(
            name="states-the-required-facts",
            passed=not omissions,
            detail=(
                f"all {len(required)} required fact(s) stated"
                if not omissions
                else "; ".join(v.detail for v in omissions[:3])
            ),
        ),
        CharacteristicResult(
            name="no-unsupported-claims",
            passed=report.grounded,
            detail=report.summary(),
        ),
    ]

    if "says-there-is-nothing-to-compare" in case.expect:
        said = groundedness.must_not_say(text, list(_NO_BASELINE_FORBIDDEN))
        compares = any(
            phrase in text.lower()
            for phrase in ("no baseline", "nothing to compare", "not yet committed", "first")
        )
        results.append(
            CharacteristicResult(
                name="says-there-is-nothing-to-compare",
                passed=compares and not said,
                detail=(
                    said[0].detail
                    if said
                    else (
                        "says there is no baseline"
                        if compares
                        else "never mentions the absent baseline"
                    )
                ),
            )
        )

    if "does-not-manufacture-activity" in case.expect:
        invented = groundedness.must_not_say(text, list(_QUIET_WEEK_FORBIDDEN), negation_aware=True)
        results.append(
            CharacteristicResult(
                name="does-not-manufacture-activity",
                passed=not invented,
                detail=invented[0].detail if invented else "reports the quiet week as quiet",
            )
        )

    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=usage,
        observations={
            # The output itself. A golden suite whose failures cannot be read
            # afterwards is half a suite: this case failed once on a phrase that
            # a regeneration did not reproduce, and without the text there was
            # nothing to diagnose.
            "output": text,
            "claims_checked": report.checked,
            "violations": len(report.violations),
            "required_facts": len(required),
            "omitted_facts": len(omissions),
        },
    )


def _facts_for(case: Case) -> StatusFacts:
    return FACT_SETS[case.input["fact_index"]]


def run(case: Case, tmp_root: Path, client=None) -> CaseResult:
    """Generate with the shipped agent, then score."""
    facts = _facts_for(case)
    started = time.perf_counter()
    try:
        agent = StatusAgent(model=get_settings().anthropic_model, client=client or _client())
        narrative = agent.run(facts)
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000
    # RC1-254: this reported $0 while spending 39s against a real model, because
    # `StatusAgent.run` returned only the parsed output. `last_usage` is the side
    # channel that fixed it — see `agents.usage`.
    used = agent.last_usage
    usage = Usage(latency_ms=latency_ms)
    if used is not None:
        usage = Usage(
            input_tokens=used.input_tokens,
            output_tokens=used.output_tokens,
            cost_usd=cost_usd(used.model, used.input_tokens, used.output_tokens),
            latency_ms=latency_ms,
        )
    return _score(case, facts, _render(narrative), usage)


def run_fallback(case: Case, tmp_root: Path) -> CaseResult:
    """The deterministic twin. Free, and it needs no credentials."""
    facts = _facts_for(case)
    started = time.perf_counter()
    narrative = fallback_narrative(facts)
    return _score(
        case, facts, _render(narrative), Usage(latency_ms=(time.perf_counter() - started) * 1000)
    )


def _render(narrative) -> str:
    return "\n".join([narrative.exec_summary, *[f"- {p}" for p in narrative.points]])


def _client():
    import anthropic

    preflight()
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
