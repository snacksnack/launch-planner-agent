"""Finalize a spec review — verification, score, verdict, rendering (RC1-290).

Everything between the agent's draft and something a person acts on, all pure
code. The LLM proposed (RC1-289); from here Python decides:

- **Quote verification.** A finding whose quote cannot be located in the source
  is *dropped, not downgraded* — a "probably real" finding with a fabricated
  quote destroys trust in the real ones. Drops are counted on the review
  (`dropped_unverifiable`), never silent: a climbing drop rate means the rubric
  started paraphrasing, and the evals watch it.
- **Readiness score.** Severity-weighted and recomputable by hand — the formula
  is `WEIGHTS`, published in docs/spec-rubric.md. A score nobody can recompute
  is a vibe with a number on it.
- **Verdict.** Gates on *category*, never severity — the PR agent's `block_on`
  posture. The default `block_on` is EMPTY: nothing in a spec review is as
  unambiguous as a committed secret, so the gate is advisory unless somebody
  deliberately opts in.
"""

from __future__ import annotations

from collections.abc import Iterable

from planner_core.spec_gate.models import (
    FindingCategory,
    SpecReview,
    SpecSeverity,
    SpecVerdict,
)
from planner_core.spec_gate.text import normalize_for_quote_match

#: Score penalty per finding, by severity. Applied to rubric and structural
#: findings alike; the readiness score is 1 - sum(penalties), floored at 0.
WEIGHTS: dict[SpecSeverity, float] = {
    SpecSeverity.BLOCKER: 0.25,
    SpecSeverity.WARNING: 0.05,
    SpecSeverity.NIT: 0.01,
}

#: Advisory by default — deliberately empty. See module docstring.
DEFAULT_BLOCK_ON: frozenset[FindingCategory] = frozenset()


def verify_quotes(review: SpecReview, source_text: str) -> SpecReview:
    """Drop findings whose quote is not verbatim in the source; count the drops.

    Matching goes through `normalize_for_quote_match`, so faithful quotes that
    the source wraps or decorates still verify (the RC1-257 rules). Structural
    findings are not checked — their quotes are source slices by construction.
    """
    haystack = normalize_for_quote_match(source_text)
    kept = [f for f in review.findings if normalize_for_quote_match(f.quote) in haystack]
    return review.model_copy(
        update={
            "findings": kept,
            "dropped_unverifiable": review.dropped_unverifiable
            + (len(review.findings) - len(kept)),
        }
    )


def readiness_score(review: SpecReview) -> float:
    """1.0 minus the severity-weighted penalty of every finding, floored at 0."""
    penalty = sum(WEIGHTS[f.severity] for f in review.findings) + sum(
        WEIGHTS[f.severity] for f in review.structural_findings
    )
    return max(0.0, round(1.0 - penalty, 4))


def decide_verdict(
    review: SpecReview, block_on: Iterable[FindingCategory] = DEFAULT_BLOCK_ON
) -> SpecVerdict:
    """BLOCKED only when a *surviving* finding's category is in `block_on`."""
    blocked = frozenset(block_on)
    if any(f.category in blocked for f in review.findings):
        return SpecVerdict.BLOCKED
    return SpecVerdict.ADVISORY


def finalize_review(
    review: SpecReview,
    source_text: str,
    block_on: Iterable[FindingCategory] = DEFAULT_BLOCK_ON,
) -> SpecReview:
    """Verify quotes, then score and decide — in that order, so a dropped
    finding can neither block nor depress the score."""
    verified = verify_quotes(review, source_text)
    return verified.model_copy(
        update={
            "readiness_score": readiness_score(verified),
            "verdict": decide_verdict(verified, block_on),
        }
    )


def render_review_markdown(review: SpecReview) -> str:
    """Human rendering of a finalized review. JSON is `model_dump_json` on the
    same object — one source of truth, two renderings."""
    lines = [
        f"# Spec review — {review.verdict.value.upper()}",
        "",
        f"Source: {review.source_document or '(unspecified)'}",
        f"Readiness score: {review.readiness_score if review.readiness_score is not None else 'unscored'}",  # noqa: E501
        f"Findings: {len(review.findings)} rubric, {len(review.structural_findings)} structural"
        + (
            f" ({review.dropped_unverifiable} dropped: quote not found in source)"
            if review.dropped_unverifiable
            else ""
        ),
    ]
    if review.structural_findings:
        lines += ["", "## Structural findings"]
        for f in review.structural_findings:
            where = f" ({f.section})" if f.section else ""
            lines.append(f"- **{f.severity.value}** `{f.code}`{where}: {f.message}")
            if f.quote:
                lines.append(f'  > "{f.quote}"')
    if review.findings:
        lines += ["", "## Findings"]
        for f in review.sorted_findings:
            where = f" ({f.section})" if f.section else ""
            lines.append(f"- **{f.severity.value}** `{f.category.value}`{where}")
            lines.append(f'  > "{f.quote}"')
            lines.append(f"  {f.explanation}")
            if f.suggested_rewrite:
                lines.append(f"  Suggested rewrite: {f.suggested_rewrite}")
    if review.questions_for_author:
        lines += ["", "## Questions for the author"]
        lines += [f"- {q}" for q in review.questions_for_author]
    if not review.findings and not review.structural_findings:
        lines += ["", "No findings — a clean review is a valid, complete answer."]
    return "\n".join(lines) + "\n"
