"""RAID goldens — planted-risk recall, and the noise it costs (RC1-257).

The most honest metric in the harness, and the one the ticket asks for: did it
find the risk we hid. No rubric, no judge — a risk the PRD states outright either
appears in the log or it does not.

## Recall alone is a metric you can game by flagging everything

So it is never reported alone. The low-risk PRD is the precision proxy: a routine
quarterly dependency bump, eleventh run of an unchanged runbook, no deadline, a
two-minute rehearsed rollback, internal users, no SLA. An agent that returns a
long risk register for that is not being careful, it is being useless — and it is
the same failure as a code reviewer who flags every diff.

The two numbers are kept apart in the report and in the run record. Merging them
into one score is how a suite stops being able to tell diligence from noise.

## Why noise is gated but recall's ceiling is not

There is no defensible upper bound on how many risks a genuinely risky migration
has, so the flagship case gates only on the risks the PRD *states* — a floor, not
a target. The low-risk case gates on a ceiling, because "how much should you say
about a version bump with a rehearsed rollback" does have a defensible answer.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from agent_evals.case import Case
from agent_evals.pricing import cost_usd
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage
from agents.raid import DEFAULT_MODEL, SYSTEM_PROMPT, RaidAgent
from app.config import get_settings
from planner_core.raid import RaidItem

from evals import planning

NAME = "raid"

#: See `work_breakdown.PROMPT_CONTRACT` for why this exists (RC1-255).
PROMPT_CONTRACT: tuple[tuple[str, str], ...] = (
    ("probability", "scores-risks-it-raises"),
    ("impact", "scores-risks-it-raises"),
)

#: Risks the flagship PRD states outright. Each is a tuple of alternatives — any
#: one of them counts as having surfaced it, because the eval is checking that
#: the *risk* was found, not that it was named a particular way.
_MUST_SURFACE: dict[str, tuple[tuple[str, ...], ...]] = {
    "jira-cloud-migration": (
        ("plugin", "app compat", "marketplace"),
        ("data", "migrat"),
        ("cutover", "downtime", "go-live"),
    ),
}

#: What a rehearsed, deadline-free, internal-only version bump can justify.
#:
#: Bounded on **risks and their severity, not on total items** — the first
#: version of this check capped the whole log at four entries and failed a
#: perfectly proportionate answer: two assumptions the PRD states outright, two
#: risks at severity 4 of 25, and one decision. Assumptions and decisions are
#: the A and D of RAID; counting them as noise punishes the agent for doing the
#: job. What would actually be wrong here is a *severe* risk, and there is a
#: defensible bound on that.
_LOW_RISK_MAX_RISKS = 3
_LOW_RISK_MAX_SEVERITY = 9  # 3x3 — nothing above "moderate" on a rehearsed bump

CASES: tuple[Case, ...] = (
    Case(
        id="jira-cloud-migration",
        input={"prd": "jira-cloud-migration"},
        expect=("surfaces-the-stated-risks", "scores-risks-it-raises"),
        tags=("raid", "flagship"),
    ),
    Case(
        id="low-risk",
        input={"prd": "low-risk"},
        expect=("stays-proportionate-on-low-risk", "scores-risks-it-raises"),
        tags=("raid", "precision-proxy"),
    ),
)


def preflight() -> None:
    if not get_settings().anthropic_api_key:
        raise RuntimeError("LPA_ANTHROPIC_API_KEY is not set. This subject drives a real model.")


def prompt_version() -> str:
    return f"raid-sha256:{hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]}"


def version() -> SubjectVersion:
    from mcp_server import __version__ as code_version

    return SubjectVersion(
        subject=NAME,
        code_version=code_version,
        model=get_settings().anthropic_model or DEFAULT_MODEL,
        prompt_version=prompt_version(),
    )


def _text(items: list[RaidItem]) -> str:
    return " ".join(f"{i.title} {i.description} {i.mitigation or ''}" for i in items).lower()


def _recall(case: Case, items: list[RaidItem]) -> CharacteristicResult:
    haystack = _text(items)
    required = _MUST_SURFACE.get(case.id, ())
    missed = [alts for alts in required if not any(a in haystack for a in alts)]
    found = len(required) - len(missed)
    return CharacteristicResult(
        name="surfaces-the-stated-risks",
        passed=not missed,
        detail=(
            f"found {found}/{len(required)} stated risk(s)"
            if not missed
            else f"missed: {'; '.join('/'.join(a) for a in missed)}"
        ),
    )


def _proportionate(items: list[RaidItem]) -> CharacteristicResult:
    risks = _risks(items)
    worst = max((r.severity or 0 for r in risks), default=0)
    over = []
    if len(risks) > _LOW_RISK_MAX_RISKS:
        over.append(f"{len(risks)} risks exceeds {_LOW_RISK_MAX_RISKS}")
    if worst > _LOW_RISK_MAX_SEVERITY:
        over.append(f"severity {worst} exceeds {_LOW_RISK_MAX_SEVERITY} on a rehearsed bump")
    return CharacteristicResult(
        name="stays-proportionate-on-low-risk",
        passed=not over,
        detail=(
            f"{len(risks)} risk(s), worst severity {worst}/25, "
            f"{len(items) - len(risks)} assumption/decision/issue entries — proportionate"
            if not over
            else "; ".join(over)
        ),
    )


def _risks(items: list[RaidItem]) -> list[RaidItem]:
    return [i for i in items if getattr(i.type, "value", str(i.type)) == "risk"]


def _scored(items: list[RaidItem]) -> CharacteristicResult:
    """A risk without probability and impact cannot be ranked, so it cannot be
    triaged — which is most of what a RAID log is for.

    Only risks are checked: assumptions, issues and dependencies have no scoring
    fields in the schema, and demanding them would fail correct output.
    """
    risks = _risks(items)
    unscored = [i.id for i in risks if i.probability is None or i.impact is None]
    return CharacteristicResult(
        name="scores-risks-it-raises",
        passed=not unscored,
        detail=(
            f"all {len(risks)} risk(s) scored"
            if not unscored
            else f"{len(unscored)}/{len(risks)} risk(s) unscored: {', '.join(unscored[:3])}"
        ),
    )


def run(case: Case, tmp_root: Path, client=None) -> CaseResult:
    name = case.input["prd"]
    prd = planning.prd_text(name)
    roster = planning.team(name)
    started = time.perf_counter()
    try:
        agent = RaidAgent(model=get_settings().anthropic_model, client=client or _client())
        # No schedule facts: this subject scores what the agent reads *from the
        # PRD*. Feeding it deterministic schedule signals would let it score by
        # restating its input, and the schedule-fact path is already covered by
        # planner_core's own tests.
        items = agent.run(prd, [], roster)
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    results = []
    if "surfaces-the-stated-risks" in case.expect:
        results.append(_recall(case, items))
    if "stays-proportionate-on-low-risk" in case.expect:
        results.append(_proportionate(items))
    results.append(_scored(items))

    by_type: dict[str, int] = {}
    for item in items:
        key = getattr(item.type, "value", str(item.type))
        by_type[key] = by_type.get(key, 0) + 1
    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=_usage(agent, latency_ms),
        observations={
            "items": len(items),
            "by_type": by_type,
            "titles": [i.title for i in items],
            "max_severity": max((i.severity or 0 for i in items), default=0),
        },
    )


def _usage(agent: RaidAgent, latency_ms: float) -> Usage:
    used = getattr(agent, "last_usage", None)
    if used is None:
        return Usage(latency_ms=latency_ms)
    return Usage(
        input_tokens=used.input_tokens,
        output_tokens=used.output_tokens,
        cost_usd=cost_usd(used.model, used.input_tokens, used.output_tokens),
        latency_ms=latency_ms,
    )


def _client():
    """Built here rather than left to the agent's default.

    `Agent._default_client()` constructs `anthropic.Anthropic()` bare, which
    reads `ANTHROPIC_API_KEY` — but this repo's key is `LPA_ANTHROPIC_API_KEY`,
    so the default authenticates only by coincidence on a machine that happens
    to have both set. Passing the resolved key is the same thing
    `subjects/status_narrative.py` does, for the same reason.
    """
    import anthropic

    preflight()
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
