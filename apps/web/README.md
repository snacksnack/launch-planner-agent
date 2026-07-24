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

## Notes

- Library choice (frappe-gantt vs vis-timeline) is recorded in
  `docs/decisions.md` (ADR-0011).
- The frontend is not part of the Python CI matrix; it's verified in the browser.
- Freeze-window shading is wired but inert until blackout windows land (RC1-196);
  milestones show projected dates once linked into the graph (RC1-198).
