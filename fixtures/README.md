# fixtures

Sample input corpora the planner runs against, plus the hand-reviewed **golden
expectations** used to eyeball and regression-test agent output. Fixtures are
plain data consumed by `apps/api`, the tests, and the demo seed — not a Python
package.

## Corpora

- **`jira-cloud-migration/`** — the flagship fixture: a realistic, deliberately
  messy PRD modeled on the on-prem Jira → Jira Cloud migration led at Marigold
  (20 projects, 15 teams, contractor budget, plugin incompatibilities, ticket
  cutoffs), with buried constraints.
- **`product-launch/`** — a smaller mobile-app launch fixture, included to prove
  the tooling isn't overfit to one document.

## Layout (per corpus)

```text
<corpus>/
  prd.md                      # the messy prose PRD — the primary input
  team.json                   # roster: list[TeamMember] (human input, no provenance)
  constraints.json            # list[Constraint] — the hard/gate constraints, provenance
                              #   cites the PRD sentence each was lifted from
  golden/
    expected-plan.json        # the hand-reviewed golden baseline (see below)
```

## What "inputs" vs "golden" mean

- **Inputs** are what you feed the system: `prd.md`, the `team.json` roster, and
  the `constraints.json` givens.
- **`golden/expected-plan.json`** is a **complete, self-contained `Plan`** — the
  epics, tasks, dependencies, and milestones a competent TPM would extract from
  the PRD, *plus* the same team and constraints from the input files. It is the
  single-file evaluation baseline: later tickets diff agent output against it.
  A test asserts `plan.team` / `plan.constraints` equal the sidecar files, so the
  duplication can't drift.

## Provenance conventions

Every agent-extractable entity carries a provenance block (see
[`planner_core.provenance`](../packages/planner-core/planner_core/provenance.py)).
In these hand-authored fixtures:

- `source_quote` is **verbatim** from the corpus's own `prd.md`. A test
  (`test_provenance_quotes_are_verbatim_from_the_prd`) enforces this, comparing
  with whitespace normalized so PRD line-wrapping doesn't matter.
- `model` is set to **`golden-baseline`** on extracted entities (epics/tasks/
  dependencies/milestones) to make clear these are hand-authored *expectations*,
  not the output of a real LLM run.
- Human-provided constraints use `agent: "human"`, `model: "human"`.
- `confidence` reflects how explicitly the PRD states a thing: `high` when it's
  spelled out, `medium`/`low` when a competent TPM would infer it (e.g. a cutover
  rehearsal, a project closeout).

## Blackout windows (RC1-196)

Freeze windows are first-class: a `Constraint` of type `blackout` carries a
machine-readable `window_start` / `window_end`. The jira-cloud-migration Q4 change
freeze (`con-freeze`, 2026-11-15 → 2027-01-04) uses it — the scheduler treats those
days as non-working and routes work around them, and the Gantt shades the window.
*(Earlier fixtures modeled the freeze as a gate with the dates in free text; that
workaround is gone.)*

## Validation

`packages/planner-core/tests/test_fixtures.py` discovers every corpus
automatically and asserts, for each: inputs load through the P1.2 model, the
golden `Plan` round-trips through JSON, all id references resolve (owners, epics,
dependency endpoints, constraint targets), the dependency graph is acyclic, and
every provenance quote is verbatim from the PRD.
