# The Spec Quality Gate — gate the spec, then plan it

The upstream gate of the pipeline (RC1-229): review a PRD *before* anyone
plans against it, then hand the gated spec straight to the planner. The
planner's provenance chain is only as good as the document it quotes from;
this is the tool that gates that input.

```
             ┌────────────── spec gate ──────────────┐
parse ──▶ structural checks ──▶ rubric ──▶ verify / score / verdict ──▶ breakdown ──▶ …
(RC1-286)    (RC1-288, free)   (RC1-289)          (RC1-290)            (the planner)
```

![Spec Quality Gate demo](spec-gate-demo.gif)

*(Real commands, real outputs — replayed for pacing; provenance in
[demo/spec-gate/README.md](demo/spec-gate/README.md).)*

## One command

```bash
uv run plan spec gate fixtures/jira-cloud-migration/          # gate, then plan
uv run plan spec review path/to/spec.md                       # gate only
```

`spec gate` reviews `<fixture>/prd.md`, prints the review, writes it as a
sidecar next to the plan (`plan.spec-review.json`, the `decisions_sidecar`
pattern — beside the plan so the plan's content hash stays clean, while a plan
can still answer *"what shape was the spec this came from"*), and then runs
**the same `cmd_breakdown` code path as `plan breakdown`**. The gate is
additive by construction: a test asserts a gated run and a plain breakdown
produce byte-identical plans.

## The advisory posture

The gate scores and suggests; it never rejects a document by default. It
refuses to plan only when a `--fail-on` category *survives quote
verification* — and `--fail-on` defaults to empty, because no spec-review
category is as unambiguous as the PR agent's `leaked_secret`. A dropped
(fabricated-quote) finding can neither block nor depress the score; ordering
is enforced in `spec_gate.report.finalize_review`.

The gate verdict is **not** recorded on the `Plan` model. The sidecar carries
the full review, verdict included; a plan-level copy would be a second source
of truth that drifts. (Considered and declined in RC1-293.)

## The `source_document` constraint

Everything downstream treats `Plan.source_document` as a **filesystem path**:
`resolve_prd` re-reads it for `dependencies`, `raid`, and `status`, and
`flag_unverifiable_quotes` needs that text again to verify every quote in the
plan. A gated spec must therefore be *stably retrievable, not merely readable
once*. If the deferred Confluence/Notion adapters ever land, do **not** write
a URL or page id into `source_document` — three later commands would quietly
degrade, and the failure would look like "dependencies stopped finding
quotes", not like "ingestion changed". Materialize the remote spec to a local
file, or widen `source_document` deliberately with a resolver and its own ADR.

## Where the details live

- Rubric categories, score formula, verdict policy: [spec-rubric.md](spec-rubric.md)
- The corpus and what is planted where: `fixtures/spec-gate/README.md`
- Quality measurement (recall per category, false positives, fabricated-quote
  rate): the `spec-structural` and `spec-review` eval subjects (RC1-292), on
  the shared [trend page](https://snacksnack.github.io/agent-evals/)
- PR surface: `.github/workflows/spec-review.yml` reviews changed spec files
  and keeps one edited-in-place comment
