# Spec-gate fixture corpus (RC1-287)

The specs the rubric is tuned against and the evals score against. Three cases,
three roles:

| File | Role |
| --- | --- |
| `vague-spec.md` | Planted-bad. Every defect is catalogued in `golden-findings.json` with its verbatim quote — recall is measured against this list. |
| `good-spec.md` | Curated-good. Different subject and structure from the vague spec (not the vague spec minus edits, which would teach the rubric to detect edits). Its golden finding list is empty; anything flagged here is a false positive and a scored miss. |
| `../jira-cloud-migration/prd.md` | Honest middle. Written for the work-breakdown agent to be realistically messy; deliberately **unlabeled** — whatever the gate says about it is a real finding, not a graded answer. |

## What is planted where (vague-spec.md)

At least two per rubric category, thirteen total. The hard ones are marked ★.

- **ambiguous_quantifier** — "must be fast" (Goals); ★ "under a second"
  (Requirements — reads precise, has no percentile or load condition); "a large
  number of concurrent sessions" (Requirements)
- **untestable_criterion** — "feel seamless to end users" (Goals);
  "Documentation will be improved" (Requirements)
- **missing_nfr** — ★ no rollback anywhere, anchored to the big-bang cutover
  sentence (Rollout); no security/session-invalidation/availability
  requirements, anchored to "the new IdP will handle all authentication"
  (Requirements)
- **unstated_assumption** — "Once the Okta tenant is provisioned by IT"
  (Rollout); "Mobile clients will pick up the new flow automatically" (Rollout)
- **conflicting_requirement** — ★ two cross-section pairs, each recorded as one
  finding quoting one clause and naming the other in its reasoning:
  90-day legacy availability (Requirements) vs immediate decommission (Rollout);
  single-weekend cutover (Rollout) vs department-at-a-time Q4 (Timeline)
- **unowned_scope** — ★ "The platform team will own the migration runbook"
  (role, not a person — the realistic version); "Training materials will be
  produced before launch" (passive, no producer)

Also planted for the structural checks (RC1-288): `Success metrics: TBD.`
(Timeline), requirements as prose paragraphs with no REQ-IDs, and no
acceptance-criteria or non-goals sections. Their golden entries
(`structural_findings`) are added in RC1-288, once the check codes exist —
guessing codes before the checks are written would just be a second place to
get them wrong.

## Maintenance rules

- `golden-findings.json` is generated **through the `planner_core.spec_gate`
  models**, so it cannot drift from the schema.
- A test asserts every golden quote appears verbatim (via
  `normalize_for_quote_match`) in its source document — editing either spec's
  prose can break it, exactly like the plan goldens in `test_fixtures.py`.
- All names are fictional; fixtures only, per the platform's people-data rule.
