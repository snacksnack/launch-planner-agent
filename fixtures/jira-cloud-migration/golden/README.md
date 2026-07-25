# Golden baseline — jira-cloud-migration

`expected-plan.json` is the hand-authored, hand-reviewed answer key for the
flagship fixture: the plan a competent TPM would extract from `../prd.md`. It is
the evaluation baseline the Work Breakdown (P1.4) and Dependency (P1.5) agents
are measured against.

## Shape

- **6 epics** — Assessment & Planning, Cloud Foundation & Security, Plugin &
  Integration Remediation, Data Migration, Cutover & Go-Live, Decommission &
  Closeout.
- **24 tasks** with owners (from `../team.json`) and PERT three-point estimates.
- **32 dependencies** forming an acyclic graph from inventory → planning →
  foundation → pilot → bulk → cutover → decommission → closeout. 28 are
  task → task; the last 4 link each milestone to the task that completes it
  (`dep-validation-mspilot`, `dep-bulk-msbulk`, `dep-cutover-msgolive`,
  `dep-decom-msdecom`) so the scheduler can project the milestone dates (RC1-198).
- **4 milestones** (pilot / bulk / go-live / decommission) and **6 constraints**
  (mirrored from `../constraints.json`).

## The judgment calls a reviewer should sanity-check

These are the interesting extractions — where the PRD is messy and a competent
TPM adds or infers structure. They carry lower confidence on purpose:

- **`task-migration-plan`** (medium) — a runbook/wave plan isn't a named
  deliverable; it's inferred from "prove the runbook end to end".
- **`task-migration-tooling`** (medium) — the Cloud Migration Assistant is never
  named; bulk-migrating 18 projects implies tooling must be stood up.
- **`task-cutover-rehearsal`** (low) — a dry run before a 20-project cutover is
  standard practice, not stated.
- **`task-closeout`** (low) — a retro/closeout is inferred, not in the PRD.
- **Buried gates** the WBS must not miss: legal sign-off before *any* client data
  moves (applies to bulk, not just pilot — `dep-legal-bulk`), the Q4 freeze
  blackout (`con-freeze`), contractor budget approval before onboarding
  (`dep-budget-onboard`), and the plugin sign-off gating user migration
  (`dep-signoff-user`).

## Regenerating / editing

Edit the JSON by hand — it *is* the reviewed artifact. After any edit, run
`uv run pytest packages/planner-core/tests/test_fixtures.py`, which re-checks the
model round-trip, referential integrity, acyclicity, and that every
`source_quote` is still verbatim in `../prd.md`.
