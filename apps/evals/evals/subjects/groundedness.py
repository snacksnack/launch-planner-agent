"""Groundedness over the committed narrative corpus — free, and it gates.

RC1-251 asks for a per-subject hallucination rate reported over time rather than
a boolean. This is the subject that produces it.

Every case is one of the 36 status narratives in `apps/evals/calibration`,
scored by `agent_evals.groundedness` — deterministic, no model, no tokens, no
credentials. That matters twice over: it can run on every push in CI, and unlike
the judge (advisory at weighted kappa 0.66, RC1-250) **it is trustworthy enough
to fail a build**.

## Why a frozen corpus rather than fresh generation

Fresh output would measure the *agent* drifting; a frozen corpus measures the
*checker* drifting, and right now the checker is the newer and less proven of
the two. Four false positives were found in it by reading flags on this same
corpus. Freezing the inputs means a change in the reported rate has exactly one
possible cause, which is what makes the trend readable.

RC1-252 adds the fresh-generation half with must-say and must-not-say case sets.
This subject is deliberately the narrow one.

## The two variants are scored differently, on purpose

* `fallback` and `agent` outputs **must** come back clean. A flag on a template
  that restates the facts is a bug in the checker, not a finding about the
  output — so these gate.
* `degraded` outputs are known to contain unsupported claims, but only some are
  *deterministically* detectable; the rest are soft claims ("the team has
  absorbed the slip") that need the judge. So detection there is reported and
  **advisory**, never gating. Asserting per-seed recall would be asserting that
  a deterministic layer can catch a semantic problem, which it cannot.
"""

from __future__ import annotations

import time
from pathlib import Path

from agent_evals import groundedness as scorer
from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage
from agent_evals.seeds import Seed, SeedStore

from evals.config import SEEDS_PATH

NAME = "groundedness"

#: Clean variants gate; the planted one only reports.
_CLEAN = ("fallback", "agent")

ADVISORY = frozenset({"planted-degradation-is-detected"})


def _seeds() -> list[Seed]:
    return SeedStore(SEEDS_PATH).all()


def _build_cases() -> tuple[Case, ...]:
    cases = []
    for seed in _seeds():
        expect = (
            ("no-unsupported-claims",)
            if seed.variant in _CLEAN
            else ("planted-degradation-is-detected",)
        )
        cases.append(
            Case(
                id=seed.id,
                input={"seed_id": seed.id},
                expect=expect,
                tags=("narrative", seed.variant),
            )
        )
    return tuple(cases)


CASES: tuple[Case, ...] = _build_cases()


def version() -> SubjectVersion:
    from evals import __version__

    return SubjectVersion(
        subject=NAME,
        code_version=__version__,
        # Deterministic: no model, no prompt. Said explicitly rather than
        # omitted — see `SubjectVersion`.
        model=None,
        prompt_version=None,
    )


def run(case: Case, tmp_root: Path) -> CaseResult:
    seed = next((s for s in _seeds() if s.id == case.input["seed_id"]), None)
    if seed is None:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=0.0),
            error=f"seed {case.input['seed_id']!r} is not in the committed corpus",
        )

    started = time.perf_counter()
    report = scorer.check(seed.rendered_output(), seed.facts)
    latency_ms = (time.perf_counter() - started) * 1000

    if seed.variant in _CLEAN:
        characteristic = CharacteristicResult(
            name="no-unsupported-claims",
            passed=report.grounded,
            detail=report.summary(),
        )
    else:
        characteristic = CharacteristicResult(
            name="planted-degradation-is-detected",
            passed=not report.grounded,
            detail=(
                report.summary()
                if not report.grounded
                else "no deterministic violation — left to the judge"
            ),
            advisory=True,
        )

    return CaseResult(
        case_id=case.id,
        characteristics=[characteristic],
        usage=Usage(latency_ms=latency_ms),
        observations={
            "variant": seed.variant,
            "claims_checked": report.checked,
            "violations": len(report.violations),
            "violation_kinds": sorted({v.kind for v in report.violations}),
        },
    )
