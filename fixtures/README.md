# fixtures

Sample input corpora the planner runs against — **PRDs plus team/constraints
files** (meeting notes and Jira exports as inputs are future scope).

Populated in **P1.3 (RC1-184)**:

- `jira-cloud-migration/` — the flagship fixture: a realistic, deliberately
  messy PRD modeled on the on-prem Jira → Jira Cloud migration led at Marigold
  (20 projects, 15 teams), with `team.json`, `constraints.json`, and a
  hand-reviewed golden-expectations file used as the evaluation baseline.
- A second, smaller product-launch fixture to prove the tool isn't overfit to
  one document.

Fixtures are plain data consumed by `apps/api`, the tests, and the demo seed —
not a Python package.
