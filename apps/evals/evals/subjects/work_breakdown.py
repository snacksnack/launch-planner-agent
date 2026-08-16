"""Work-breakdown goldens — structure gates, restraint is the interesting case.

RC1-230 named "launch-planner work breakdowns" as a subject under test and no
story covered it until RC1-257. This is that subject.

## Most of this needs no judge

Six of the seven characteristics are exact: provenance traces to the PRD, no
dangling epic references, no duplicate ids, owners on the roster, the required
themes present, and — on the thin PRD — restraint. Only "did it propose the
right work" would need judgement, and it is not asked here, because the
`no-unsupported-claims` calibration (κ 0.86) was measured on narratives and does
not transfer to a different output shape without being re-measured.

## The thin PRD is the case that earns its keep

A two-paragraph PRD with no dates, no decisions and one sentence of scope should
produce a small, hedged breakdown. An agent that returns twelve epics from it has
invented a project, and the system prompt already says *"prefer a smaller,
defensible breakdown over an exhaustive one"* — so this is testing a promise the
prompt makes, not a preference the eval invented.

Restraint is scored as a **ceiling on volume**, not a target. There is no correct
number of epics for a thin PRD; there is a number above which the agent is
clearly making things up. The ceiling is set from what the PRD can actually
support, and the observation records the real count so the margin is visible
rather than implied.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from agent_evals.case import Case
from agent_evals.pricing import cost_usd
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage
from agents.work_breakdown import DEFAULT_MODEL, SYSTEM_PROMPT, WorkBreakdownAgent
from app.config import get_settings
from planner_core import WorkBreakdown

from evals import planning

NAME = "work-breakdown"

#: Clauses in the agent's system prompt that this subject's characteristics
#: depend on. Asserted free, in CI, by `tests/test_prompt_contracts.py`.
#:
#: RC1-255: before this, every prompt-dependent check lived in a billed subject
#: that by design never runs in CI (ADR-0031) — so a prompt edit that broke a
#: characteristic passed ruff, passed pytest, and passed the free subjects. The
#: regression was real and invisible. The same gap in `tpm-automation-platform`
#: was demonstrated by deleting a rule `evals degrade` had measured as
#: load-bearing and watching CI stay green.
PROMPT_CONTRACT: tuple[tuple[str, str], ...] = (
    ("source_quote", "traces-to-the-prd reads provenance.source_quote"),
    ("VERBATIM", "traces-to-the-prd matches the quote against the PRD"),
    ("Never invent an owner", "owners-are-on-the-roster"),
    ("smaller, defensible", "shows-restraint on the thin PRD"),
    ("optimistic <= likely", "the schema rejects a disordered estimate before scoring"),
)

#: The themes any competent breakdown of the flagship migration has to propose.
#: Matched on either of two words so the check is about the *theme* rather than
#: the wording — an epic called "Data Migration" and one called "Migrate Jira
#: Data" are the same proposal, and failing the second would be scoring prose.
_REQUIRED_THEMES: dict[str, tuple[str, ...]] = {
    "jira-cloud-migration": (
        "migrat",  # the data migration itself
        "cutover",  # the go-live
        "plugin",  # the app/plugin remediation the PRD spends a section on
    ),
    "product-launch": ("beta", "launch"),
}

#: What a two-paragraph PRD can support. Deliberately generous — this is a
#: ceiling that catches invention, not a target that rewards terseness.
_THIN_MAX_EPICS = 3
_THIN_MAX_TASKS = 8

CASES: tuple[Case, ...] = (
    Case(
        id="jira-cloud-migration",
        input={"prd": "jira-cloud-migration"},
        expect=(
            "traces-to-the-prd",
            "no-orphan-tasks",
            "no-duplicate-ids",
            "owners-are-on-the-roster",
            "proposes-the-required-themes",
        ),
        tags=("breakdown", "flagship"),
    ),
    Case(
        id="product-launch",
        input={"prd": "product-launch"},
        expect=(
            "traces-to-the-prd",
            "no-orphan-tasks",
            "no-duplicate-ids",
            "owners-are-on-the-roster",
            "proposes-the-required-themes",
        ),
        tags=("breakdown",),
    ),
    Case(
        id="thin",
        input={"prd": "thin"},
        expect=(
            "traces-to-the-prd",
            "no-orphan-tasks",
            "no-duplicate-ids",
            "owners-are-on-the-roster",
            "shows-restraint",
        ),
        tags=("breakdown", "thin"),
    ),
)


def preflight() -> None:
    if not get_settings().anthropic_api_key:
        raise RuntimeError("LPA_ANTHROPIC_API_KEY is not set. This subject drives a real model.")


def prompt_version() -> str:
    return f"wbs-sha256:{hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]}"


def version() -> SubjectVersion:
    from mcp_server import __version__ as code_version

    return SubjectVersion(
        subject=NAME,
        code_version=code_version,
        model=get_settings().anthropic_model or DEFAULT_MODEL,
        prompt_version=prompt_version(),
    )


def _required_themes(case: Case, breakdown: WorkBreakdown) -> CharacteristicResult:
    themes = _REQUIRED_THEMES.get(case.id, ())
    haystack = " ".join(
        [e.name.lower() for e in breakdown.epics] + [t.name.lower() for t in breakdown.tasks]
    )
    missing = [t for t in themes if t not in haystack]
    return CharacteristicResult(
        name="proposes-the-required-themes",
        passed=not missing,
        detail=(
            f"all {len(themes)} required theme(s) present"
            if not missing
            else f"no epic or task mentions: {', '.join(missing)}"
        ),
    )


def _restraint(breakdown: WorkBreakdown) -> CharacteristicResult:
    """A thin PRD must not produce a confident large plan.

    Volume is the measurable half of restraint. The other half — whether the
    agent *said* it was inferring — is carried by `confidence`, reported
    alongside as an observation rather than gated, because the prompt asks for
    medium/low on inferred work without specifying how much.
    """
    over = []
    if len(breakdown.epics) > _THIN_MAX_EPICS:
        over.append(f"{len(breakdown.epics)} epics exceeds the {_THIN_MAX_EPICS} a thin PRD bears")
    if len(breakdown.tasks) > _THIN_MAX_TASKS:
        over.append(f"{len(breakdown.tasks)} tasks exceeds {_THIN_MAX_TASKS}")
    return CharacteristicResult(
        name="shows-restraint",
        passed=not over,
        detail=(
            "; ".join(over)
            if over
            else f"{len(breakdown.epics)} epic(s), {len(breakdown.tasks)} task(s) — proportionate"
        ),
    )


def run(case: Case, tmp_root: Path, client=None) -> CaseResult:
    name = case.input["prd"]
    prd = planning.prd_text(name)
    roster = planning.team(name)
    started = time.perf_counter()
    try:
        agent = WorkBreakdownAgent(model=get_settings().anthropic_model, client=client or _client())
        breakdown = agent.run(prd, roster)
    except Exception as exc:
        # A schema violation lands here rather than as a failed characteristic:
        # there is no partially-valid breakdown to grade. See `evals.planning`.
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    results = [
        planning.traces_to_the_prd(breakdown.epics, breakdown.tasks, prd),
        planning.no_orphan_tasks(breakdown.epics, breakdown.tasks),
        planning.no_duplicate_ids(breakdown.epics, breakdown.tasks),
        planning.owners_are_on_the_roster(breakdown.tasks, roster),
    ]
    if "shows-restraint" in case.expect:
        results.append(_restraint(breakdown))
    else:
        results.append(_required_themes(case, breakdown))

    confidences = [t.provenance.confidence for t in breakdown.tasks]
    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=_usage(agent, latency_ms),
        observations={
            "epics": [e.name for e in breakdown.epics],
            "task_count": len(breakdown.tasks),
            # Restraint's other half, reported rather than gated — see `_restraint`.
            "confidence_mix": {
                level: sum(1 for c in confidences if str(c) == level)
                for level in ("high", "medium", "low")
            },
            "unassigned_tasks": sum(1 for t in breakdown.tasks if t.owner_id is None),
        },
    )


def _usage(agent: WorkBreakdownAgent, latency_ms: float) -> Usage:
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
