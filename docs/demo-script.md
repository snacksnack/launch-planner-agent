# 2-minute demo script

For a live walkthrough of the public demo (`planner.hihelloreid.com`) or a local
`npm run dev`. Everything below is the **read-only, credential-free** path — safe
to click through in front of anyone. Times are a guide.

---

**0:00 — The plan (15s).** Open the demo. It's a real project: an on-prem Jira →
Jira Cloud migration (20 projects, 15 teams). The bars are the schedule; the
**critical path is outlined in red** — the chain with no slack. Toggle **Critical
path only** to isolate it: this is what determines the launch date.

**0:15 — The audit trail (25s).** Click a bar — say *Bulk-migrate the remaining 18
projects*. The panel shows its dates, float, owner, and the **provenance**: the
verbatim PRD sentence that justified it and the agent's confidence. Now click
**How it's made** — the full reasoning chain: which agents proposed what, what the
deterministic validator dropped or flagged, and the human review timeline
(proposal → baseline → commit). *This is the pitch: the plan is an audit trail, not
a black box.*

**0:40 — What-if (30s).** Click **Simulate**, slip *Obtain legal sign-off* by 30
days. The timeline recomputes and shows a **ghost overlay** — current bars over the
baseline, connectors on every task that moved. The banner: *"Launch slips 24
working days"* — not 30, because legal review had 6 days of float. Legal review
**becomes critical**. This is deterministic CPM, not an LLM guess.

**1:10 — Drift & risk (25s).** **Baseline** overlays the current plan on the
committed baseline — the drift the timeline in most tools can't show. **RAID**
lists the risks, one of them *schedule-derived*: "the critical path runs through a
single owner" — a fact mined from the schedule, not the doc.

**1:35 — The exec update (25s).** **Status** is the weekly update: a **health
badge** (green/yellow/red) set **by rule** — a missed deadline or a big slip is red,
not the LLM's opinion — plus the plain-English "what changed since last week", every
line traceable to the diff. **Copy as Markdown** and it's ready to send.

**2:00 — Close.** *"LLM proposes, Python validates, human approves — with a
provenance trail from the PRD all the way into the Jira tickets it generates."*

---

## The full pipeline (with an API key, off the demo)

To show the agents actually building a plan from a PRD (needs `LPA_ANTHROPIC_API_KEY`):

```bash
uv run plan breakdown fixtures/jira-cloud-migration/     # PRD → epics + tasks
uv run plan dependencies fixtures/jira-cloud-migration/plan.json
uv run plan raid fixtures/jira-cloud-migration/plan.json --start-date 2026-08-03
uv run plan commit fixtures/jira-cloud-migration/plan.json --by "You" -m "approved"
uv run plan jira   fixtures/jira-cloud-migration/plan.json --start-date 2026-08-03  # mock preview
```

See the [HOWTO](HOWTO.md) for every verb.
