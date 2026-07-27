# apps/web

Interactive Gantt UI for launch-planner-agent (Vite + [frappe-gantt](https://frappe.io/gantt)).

Renders a **scheduled plan** from the API: bars coloured by epic, dependency
arrows, milestone markers, the critical path highlighted (with a critical-path-only
toggle), deadline markers, and a detail panel that surfaces each task's and
dependency's full provenance — the audit trail, in the UI rather than the JSON.

## Run it (two terminals)

```bash
# 1. API (serves the scheduled flagship golden by default — no credentials needed)
cd apps/api && uv run uvicorn app.main:app --reload

# 2. UI
cd apps/web && npm install && npm run dev
```

Open the Vite URL (default http://localhost:5173). Point the UI at a different
API with `VITE_API_BASE`; point the API at a different plan with
`LPA_PLAN_PATH` / `LPA_PROJECT_START_DATE` (see `apps/api/app/config.py`), or per
request via `/api/plan?plan=...&start=YYYY-MM-DD`.

## Demo path (interview test)

1. The critical path is outlined in red — toggle **Critical path only** to isolate it.
2. Click **Bulk-migrate the remaining 18 projects** (or the pilot) → the detail
   panel shows the dependency on **legal review**, quoting the buried PRD line
   *"Legal has to sign off before any client data moves to Cloud."*

## Styling

The UI is themed to the hihelloreid.com resume site (RC1-199): tokens mirror the
site's `src/index.css` (navy `--text #0b1220`, Carbon-blue `--accent #0f62fe`,
system-sans, 6–8px radii) and the `CareerTimeline` component (bar conventions,
semantic tints). Confidence badges follow the site's "amber-not-red for honest
gaps" ethos: HIGH = success green, MEDIUM = info blue, LOW = caution amber.

Layout is the canonical Gantt shape: a **fixed left task-name column** (with epic
colour accents; critical tasks bolded) aligned row-for-row with the bars, and a
scrollable timeline with clean bars/arrows (no in-bar labels). Selecting a task —
from the column or a bar — highlights both and opens the provenance panel. Row
geometry (`--header-h` / `--row-h`) is kept in sync between `main.js` and
`style.css`.

## Decisions panel (RC1-197)

The **Decisions** toolbar button (with a count badge) opens the build-time audit
from the payload's `decisions` block: edges the deterministic filter dropped and
why, edges cut to break a cycle, low-confidence extractions, unverifiable quotes,
unenforced gates, and PRD sections nothing cited. Each flag links back to its
entity's full provenance. Low-confidence tasks also carry a subtle amber flag (⚑)
in the task column — the "honest gaps" cue. For a committed snapshot the record is
the one persisted at commit time; for a raw plan file (the golden) the recomputable
half is rebuilt server-side from plan + PRD.

## RAID log (RC1-191)

The **RAID** toolbar button opens the Risks / Assumptions / Issues / Decisions log
from the payload's `raid` block. It's filterable by type and sorted by risk
severity (probability × impact), and each item shows its **dual-source evidence** —
a verbatim PRD quote (blockquote) or a ⛓ computed schedule fact (e.g. *"the
critical path runs through a single owner"*), with a confidence badge. The
schedule-derived risks come from a deterministic `analyze_schedule_risks` pass over
the CPM output, not from the LLM guessing. **Copy as Markdown** exports the
(filtered) log to the clipboard.

## Simulate — what-if analysis (RC1-190)

The **Simulate** toolbar button enters what-if mode. Compose a scenario in the
right rail — slip a task by N working days, or add/remove a dependency edge — and
each change POSTs to `/api/simulate`, which re-runs CPM on a copy of the plan and
returns the baseline + simulated schedules and a structured `ScheduleDelta`. The
timeline swaps to the simulated schedule and draws each moved task's **baseline
position as a faint dashed "ghost"** behind it, with a connector showing the
shift. A full-width banner states the launch impact in plain language (amber for a
slip, red when a deadline is breached, green when the change is absorbed by
float), and the panel lists critical-path joiners/leavers, breached deadlines, and
the tasks that moved. **Reset** (or Escape) exits back to the baseline.

## Notes

- Library choice (frappe-gantt vs vis-timeline) is recorded in
  `docs/decisions.md` (ADR-0011).
- The frontend is not part of the Python CI matrix; it's verified in the browser.
- Freeze-window shading is wired but inert until blackout windows land (RC1-196);
  milestones show projected dates once linked into the graph (RC1-198).
