"""Spec-review rubric evals — planted-defect recall per category (RC1-292).

The honest metrics for a review tool, in order:

- **Recall per category** on the planted corpus. One aggregate number would
  hide exactly the finding that matters — a rubric strong on quantifiers and
  blind to conflicts is fixable only if the blindness is visible. Each category
  is its own characteristic.
- **False positives on the good spec.** A rubric that flags everything achieves
  perfect recall and is useless. Blockers on the good spec gate hard; total
  volume gates against a ceiling set from observed behavior (the restraint
  pattern from the work-breakdown thin-PRD case).
- **Fabricated-quote rate**, from the RC1-290 drop counter. Deterministic, no
  judge. A drop rate above zero means the rubric started paraphrasing.

No judge anywhere: recall matching is category + normalized quote overlap
against the hand-authored golden, the same structure-only posture as the
planning subjects (ADR-0036). The one genuinely judgment-shaped question —
"is the suggested rewrite better than the original?" — is deliberately not
scored: the `no-unsupported-claims` calibration was measured on narratives and
does not transfer to rewrites without being re-measured (RC1-250's rule).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from agent_evals.case import Case
from agent_evals.pricing import cost_usd
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage
from agents.spec_review import DEFAULT_MODEL, RUBRIC_VERSION, SYSTEM_PROMPT, SpecReviewAgent
from app.config import get_settings
from planner_core.spec_gate import (
    FindingCategory,
    SpecReview,
    SpecSeverity,
    finalize_review,
    normalize_for_quote_match,
    parse_sections,
    run_structural_checks,
)

NAME = "spec-review"

_REPO_ROOT = Path(__file__).resolve().parents[4]
SPEC_GATE = _REPO_ROOT / "fixtures" / "spec-gate"

#: Clauses in the rubric prompt this subject's characteristics depend on
#: (RC1-255 pattern; asserted free in tests/test_prompt_contracts.py).
PROMPT_CONTRACT: tuple[tuple[str, str], ...] = (
    ("VERBATIM", "no-fabricated-quotes relies on quotes matching the spec"),
    ("cannot survive contact", "finds-a-blocker relies on the blocker bar being stated"),
    ("Do not invent problems", "false-positive-restraint on the good spec"),
    ("empty findings list", "the good spec may legitimately come back clean"),
    ("DIFFERENT sections", "recalls-conflicting_requirement needs whole-document reading"),
)

#: Findings the good spec may carry before restraint fails. Observed 3-4 on
#: rubric v1 (RC1-289 tuning runs); a ceiling that catches drift, not a target.
_GOOD_SPEC_MAX_FINDINGS = 6

#: Alternate anchor texts per golden finding, keyed by the golden's quote.
#:
#: Two consecutive live runs bought this table: run 1 recovered 10/13 plants,
#: run 2 recovered the same defects but anchored them on the *other* legitimate
#: sentence — the counterpart clause of a conflict, the decommission sentence
#: instead of the cutover sentence for the missing rollback — and recall read
#: 0/2 on categories the model had plainly caught. A conflict has two clauses
#: and either is a correct anchor; demanding the golden's one is measuring
#: anchor choice, not recall. Alternates are same-category only and declared
#: here, reviewable, rather than loosening the matcher itself. The last entry
#: exists because the structural `no-named-owner` check already covers the
#: ownership section and the rubric prompt says not to repeat recorded
#: findings — so any unowned_scope finding in that section recovers the plant.
_ALTERNATE_ANCHORS: dict[str, tuple[str, ...]] = {
    "The legacy auth stack will be decommissioned immediately after the cutover weekend": (
        "Legacy login remains available for 90 days after cutover",
    ),
    "Departments will migrate one at a time through Q4": (
        "All users will be cut over in a single weekend migration",
    ),
    "All users will be cut over in a single weekend migration": (
        "The legacy auth stack will be decommissioned immediately",
    ),
    "The new IdP will handle all authentication for internal and customer-facing apps": (
        "security has flagged credential reuse",
    ),
    "The platform team will own the migration runbook": (
        "Someone from support will draft the customer comms",
    ),
}

_RECALL_CHARACTERISTICS = tuple(f"recalls-{c.value}" for c in FindingCategory)

CASES: tuple[Case, ...] = (
    Case(
        id="vague-spec",
        input={"spec": "vague-spec.md"},
        expect=(*_RECALL_CHARACTERISTICS, "finds-a-blocker", "no-fabricated-quotes"),
        tags=("spec-gate", "rubric", "recall"),
    ),
    Case(
        id="good-spec",
        input={"spec": "good-spec.md"},
        expect=(
            "no-blockers-on-the-good-spec",
            "false-positive-restraint",
            "no-fabricated-quotes",
        ),
        tags=("spec-gate", "rubric", "precision"),
    ),
)


def preflight() -> None:
    if not get_settings().anthropic_api_key:
        raise RuntimeError("LPA_ANTHROPIC_API_KEY is not set. This subject drives a real model.")


def prompt_version() -> str:
    digest = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]
    return f"rubric-v{RUBRIC_VERSION}-sha256:{digest}"


def version() -> SubjectVersion:
    from mcp_server import __version__ as code_version

    return SubjectVersion(
        subject=NAME,
        code_version=code_version,
        model=get_settings().anthropic_model or DEFAULT_MODEL,
        prompt_version=prompt_version(),
    )


def _golden(spec: str) -> SpecReview:
    golden = json.loads((SPEC_GATE / "golden-findings.json").read_text())
    return SpecReview.model_validate(golden[spec])


def _recovered(golden_finding, produced) -> bool:
    """Category matches and the quote overlaps the golden anchor or a declared
    alternate (see `_ALTERNATE_ANCHORS`)."""
    anchors = [
        normalize_for_quote_match(a)
        for a in (golden_finding.quote, *_ALTERNATE_ANCHORS.get(golden_finding.quote, ()))
    ]
    for f in produced:
        if f.category is not golden_finding.category:
            continue
        got = normalize_for_quote_match(f.quote)
        if any(want in got or got in want for want in anchors):
            return True
    return False


def _recall_results(golden: SpecReview, review: SpecReview) -> list[CharacteristicResult]:
    results = []
    for category in FindingCategory:
        planted = [f for f in golden.findings if f.category is category]
        found = [f for f in planted if _recovered(f, review.findings)]
        results.append(
            CharacteristicResult(
                name=f"recalls-{category.value}",
                # At least one plant per category must be recovered; full
                # per-plant detail is in the observation either way.
                passed=bool(found),
                detail=f"{len(found)}/{len(planted)} planted finding(s) recovered",
            )
        )
    return results


def run(case: Case, tmp_root: Path, client=None) -> CaseResult:
    spec = case.input["spec"]
    text = (SPEC_GATE / spec).read_text()
    structural = run_structural_checks(parse_sections(text))
    started = time.perf_counter()
    try:
        agent = SpecReviewAgent(model=get_settings().anthropic_model, client=client or _client())
        review = finalize_review(agent.run(text, structural), text)
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    results: list[CharacteristicResult] = []
    golden = _golden(spec)
    blockers = [f for f in review.findings if f.severity is SpecSeverity.BLOCKER]

    if case.id == "vague-spec":
        results.extend(_recall_results(golden, review))
        results.append(
            CharacteristicResult(
                name="finds-a-blocker",
                passed=bool(blockers),
                detail=f"{len(blockers)} blocker(s); the corpus plants "
                "contradictions and a missing rollback",
            )
        )
    else:
        results.append(
            CharacteristicResult(
                name="no-blockers-on-the-good-spec",
                passed=not blockers,
                detail=(
                    "no blockers"
                    if not blockers
                    else f"{len(blockers)} blocker(s) on a spec whose golden is empty"
                ),
            )
        )
        results.append(
            CharacteristicResult(
                name="false-positive-restraint",
                passed=len(review.findings) <= _GOOD_SPEC_MAX_FINDINGS,
                detail=f"{len(review.findings)} finding(s) against a ceiling of "
                f"{_GOOD_SPEC_MAX_FINDINGS} (golden: 0 — every finding here is "
                "a false positive)",
            )
        )
    results.append(
        CharacteristicResult(
            name="no-fabricated-quotes",
            passed=review.dropped_unverifiable == 0,
            detail=f"{review.dropped_unverifiable} finding(s) dropped in quote verification",
        )
    )

    per_category = {
        c.value: sum(1 for f in review.findings if f.category is c) for c in FindingCategory
    }
    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=_usage(agent, latency_ms),
        observations={
            "findings": len(review.findings),
            "blockers": len(blockers),
            "dropped_unverifiable": review.dropped_unverifiable,
            "readiness_score": review.readiness_score,
            "by_category": per_category,
            "rubric_version": review.rubric_version,
        },
    )


def _usage(agent: SpecReviewAgent, latency_ms: float) -> Usage:
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
    """Resolved key passed explicitly — same reason as the other billed subjects."""
    import anthropic

    preflight()
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
