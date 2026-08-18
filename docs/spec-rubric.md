# The spec-review rubric (RC1-289)

What the Spec Quality Gate's LLM pass looks for, why each category is there,
and where its limits are. The rubric prompt itself lives in
`packages/agents/agents/spec_review.py` (`SYSTEM_PROMPT`), versioned by
`RUBRIC_VERSION` — bump it on any prompt change, because the evals attribute
score movement to it and an unbumped edit makes a regression unattributable.
This document explains the prompt; the prompt is the artifact.

## Where the rubric sits

```
parse sections ─▶ structural checks ─▶ rubric (this) ─▶ quote verification,
   (RC1-286)         (RC1-288)          (RC1-289)        score, verdict (RC1-290)
```

The structural findings are injected into the rubric prompt as already-recorded
context (the PR agent's precomputed-findings pattern), so the model builds on
the deterministic layer instead of re-deriving or repeating it. Everything
after the rubric is pure code: the model proposes findings; it never decides
what survives, what the readiness score is, or whether anything blocks.

## The six categories

| Category | What it catches | The guidance that matters |
| --- | --- | --- |
| `ambiguous_quantifier` | "fast", "scalable", "soon" | The hard case is a number that *reads* precise but has no percentile, population, or measurement window — "under a second" is not a requirement until you say at which percentile, under what load, measured where. |
| `untestable_criterion` | No observable pass/fail condition | The test: could two competent people disagree about whether this shipped? |
| `missing_nfr` | Absent SLO, rollback, retention, security, accessibility, cost ceiling | Weighted by what the document's subject makes conspicuous — a migration spec silent on rollback outranks a UI spec silent on retention. Absences have no text, so the quote anchors to the sentence that *raises* the expectation. |
| `unstated_assumption` | Dependencies never declared | Another team's deliverable, a version assumed deployed, an approval assumed granted. |
| `conflicting_requirement` | Two clauses that cannot both hold | They usually live in different sections, so the prompt demands whole-document reading. One finding per conflict: quote one clause, name the other (and its section) in the reasoning. |
| `unowned_scope` | No accountable individual | Role-only ("the platform team") and passive voice ("materials will be produced") both qualify. The structural layer only checks whether *any* individual is named; judging whether a named team counts is this category's job. |

## Severity

`blocker` — planning against the spec produces a plan that cannot survive
contact (contradictions; a missing NFR the subject makes critical).
`warning` — a competent reviewer sends the document back. `nit` — worth
fixing, not blocking. Same ladder as the PR agent; the verdict never gates on
severity, only on category (RC1-290).

## The readiness score and the verdict (RC1-290)

Both are pure code over the *surviving* findings — quote verification runs
first, so a dropped finding can neither block nor depress the score.

The score is recomputable by hand: `1 − Σ penalty`, floored at 0, where the
penalty is 0.25 per blocker, 0.05 per warning, and 0.01 per nit (structural
findings included, same weights — `spec_gate.report.WEIGHTS`). Severity-only
on purpose: category carries no score weight, because category is what the
*verdict* gates on, and giving it both jobs would make neither explainable.

The verdict gates on category, never severity — a blocker-severity ambiguous
quantifier never blocks, a category in `block_on` always does. `block_on`
defaults to **empty**: unlike the PR agent's `leaked_secret`, no spec-review
category is unambiguous enough to justify blocking someone's document by
default, so the gate is advisory unless a caller deliberately opts in.

## Assumptions and limits, stated

- **Findings must quote verbatim.** The prompt says a finding that cannot be
  anchored to a quote does not exist, and RC1-290 enforces it: unverifiable
  quotes are dropped and counted. The drop count is a rubric health metric —
  if it climbs, the rubric started paraphrasing.
- **The rubric sees prose, not truth.** It can flag that a claim is untestable
  or contradicted *within the document*; it cannot know whether the claim is
  factually right.
- **An empty review is a valid answer.** The prompt states it, and the good
  fixture (`fixtures/spec-gate/good-spec.md`) holds it to that: every finding
  on that document is scored as a false positive by the evals (RC1-292).
- **Advisory posture.** Suggested rewrites are proposals; the default
  configuration never blocks anyone's document.

## Measurement

Recall per category against the planted corpus, false positives on the good
spec, and the dropped-quote rate are measured by the `spec-review` eval
subject (RC1-292), with runs attributed to `RUBRIC_VERSION` + model id. The
tuning history lives with the eval results, not in this file.
