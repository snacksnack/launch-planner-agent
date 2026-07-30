# HOWTO — using launch-planner-agent

A task-oriented guide: how to **run it**, **use every CLI verb**, and **read every
UI surface**. For *why* it's built the way it is, see
[architecture.md](architecture.md) and the [decision log](decisions.md); for the
"agent's plan vs. what actually happened" story, see [case-study.md](case-study.md).

Everything here runs **credential-free** against the flagship golden plan
(`fixtures/jira-cloud-migration/`) unless a step is marked **needs an API key**.

> **Prefer a visual tour?** A one-page, brand-matched
> [visual quick-start](https://claude.ai/code/artifact/89cfe001-3652-4ffd-85f1-815836d83031)
> walks the pipeline, the two-terminal setup, and the seven dashboard surfaces.
> (Private by default — shareable from the page's share menu.)

---

## 1. Quick start (two terminals, no credentials)

Requires [uv](https://docs.astral.sh/uv/) (fetches Python 3.12) and Node 18+.

```bash
uv sync --all-packages                      # one venv for the whole workspace

# Terminal 1 — the API (serves the scheduled golden plan by default)
uv run uvicorn app.main:app --reload        # http://localhost:8000

# Terminal 2 — the web UI
cd apps/web && npm install && npm run dev    # http://localhost:5173
```

Open **http://localhost:5173**. You should see the *On-Prem Jira → Jira Cloud
Migration* plan rendered as a Gantt with the critical path outlined in red. No API
key is needed — the UI reads the pre-scheduled golden from `GET /api/plan`.

> If you run the API on a non-default port, point the UI at it:
> `VITE_API_BASE=http://localhost:8001 npm run dev`.

**Checks** (from the repo root):

```bash
uv run ruff check .          # lint
uv run lint-imports          # enforce: planner-core imports no LLM/app code
uv run pytest                # the Python test suite
(cd apps/web && npm test)    # the frontend unit tests (Vitest)
```

---

## 2. The web UI, surface by surface

Every panel opens in the right rail. The toolbar (top-right) holds the toggles and
the panel buttons.

### Gantt (the canvas)
- **Critical path** is outlined in red; the left column bolds critical tasks.
- **Dependencies** toggle — dependency arrows are off by default for a clean read;
  turn them on to see the edges. **Critical path only** dims everything else.
- **Day / Week / Month** changes the time scale.
- **Click any bar or task row** → the detail panel shows its dates, float, owner,
  and the **provenance** (reasoning + verbatim PRD quote + confidence) — the audit
  trail, in the UI rather than the JSON.

### Decisions — *what the agents decided, what Python did*
The **Decisions** button (with a count badge) opens the build-time audit: edges the
deterministic filter **dropped** and why, edges **cut to break a cycle**,
**low-confidence** extractions, unverifiable quotes, and PRD sections nothing
cited. Each flag links to the entity. Low-confidence tasks also carry a subtle
amber ⚑ in the task column.

### RAID — *risks, assumptions, issues, decisions*
The **RAID** button opens the RAID log, filterable by type and sorted by risk
severity (probability × impact). Each item shows its **evidence** — a verbatim PRD
quote *or* a ⛓ computed schedule fact (e.g. "the critical path runs through a
single owner"). **Copy as Markdown** exports it.

### Simulate — *what-if analysis*
The **Simulate** button enters what-if mode. Compose a scenario (slip a task N days,
add/remove a dependency); the schedule recomputes and the timeline shows the
**ghost overlay** — current bars over a faint dashed ghost of the baseline
positions, with connectors showing each shift. A banner states the launch impact;
the panel lists critical-path joiners/leavers and every moved task. **Reset** exits.

*Example:* slip **Obtain legal sign-off** 30 days → "Launch slips 24 working days"
(its 6 days of float absorbed the rest), and legal review becomes critical.

### Baseline — *plan vs. the committed baseline*
The **Baseline** button overlays the current bars on a ghost of the committed
baseline and shows the drift (tasks that moved, structural changes). If no baseline
exists yet, it prompts you to commit one (see the CLI below).

### Status — *the weekly exec update*
The **Status** button shows the update vs the baseline: a **health badge**
(green/yellow/red, set **by rule** not the LLM), an executive summary, and the
"what changed since last week" list — all derived from the changed-since diff.
**Copy as Markdown** exports it.

### Jira — *generate tickets (mock preview)*
The **Jira** button previews exactly what real mode would create: epics, stories
(with due dates + labels), and dependency → "blocks" links, each description
carrying the provenance. Check/uncheck issues (partial approval); the panel shows
the gated CLI command for your selection. **The web UI never writes to Jira** — a
real run is an explicit CLI step (§4).

---

## 3. The CLI — `plan`

`uv run plan <verb> …`, run from the repo root. Paths are repo-relative.
`GOLDEN=fixtures/jira-cloud-migration/golden/expected-plan.json` in the examples.

### Deterministic verbs (no key)

```bash
# Schedule: CPM, float, critical path, deadline checks
uv run plan schedule $GOLDEN --start-date 2026-08-03

# Simulate a slip / dependency edit and print the schedule delta
uv run plan simulate $GOLDEN --start-date 2026-08-03 --slip task-legal-review:30
uv run plan simulate $GOLDEN --start-date 2026-08-03 --add-dep task-a:task-b

# Generate Jira issues — MOCK preview (no writes, no credentials)
uv run plan jira $GOLDEN --start-date 2026-08-03 --project PMA
```

### The plan-of-record store (no key)

```bash
uv run plan commit    $GOLDEN --by "Priya Nair" -m "reviewed & approved"
uv run plan baseline  $GOLDEN --by "Priya Nair" --note "initial plan"   # set a baseline
uv run plan variance  $GOLDEN --start-date 2026-08-03                    # drift vs latest baseline
uv run plan status    $GOLDEN --start-date 2026-08-03                    # weekly update (Markdown)
uv run plan status    $GOLDEN --start-date 2026-08-03 --html            # …as an HTML email body
uv run plan history                                                     # list snapshots
uv run plan show 1                                                       # print snapshot v1's plan
uv run plan diff 1 2                                                     # structural diff of two versions
uv run plan propose   $GOLDEN -m "agent proposal"                        # record a proposal to diff a commit against
```

`variance` and `status` need a baseline first (`plan baseline …`); on an empty
store they tell you so.

> **Shared store.** The plan store is a SQLite file resolved *relative to the
> current directory* (`LPA_DATABASE_URL`, default `./launch_planner.db`). Run the
> API and the CLI from the **same directory** (or set `LPA_DATABASE_URL` to an
> absolute path) so a baseline you commit on the CLI shows up in the UI's Baseline
> and Status panels.

### LLM verbs (**needs an API key** — `export LPA_ANTHROPIC_API_KEY=sk-…`)

These are the "LLM proposes" steps; each writes provenance and is validated by
Python. Point them at a **fixture directory** (PRD + team + constraints):

```bash
# PRD → work breakdown (epics + tasks) → plan.json
uv run plan breakdown fixtures/jira-cloud-migration/

# infer dependencies over that plan.json (writes a decisions.json sidecar too)
uv run plan dependencies fixtures/jira-cloud-migration/plan.json

# RAID log from the PRD + the computed schedule facts
uv run plan raid fixtures/jira-cloud-migration/plan.json --start-date 2026-08-03
```

### End-to-end (PRD → approved plan → status), credential-free variant

Because breakdown/dependencies/raid need a key, the no-key walkthrough starts from
the golden (which *is* a hand-reviewed plan) and exercises the rest:

```bash
uv run plan baseline $GOLDEN --by "You" --note "initial optimistic plan"
# …edit the plan (or point at a later version), then:
uv run plan variance $GOLDEN --start-date 2026-08-03      # what drifted
uv run plan status   $GOLDEN --start-date 2026-08-03      # the exec update
uv run plan jira     $GOLDEN --start-date 2026-08-03      # the tickets it would create (mock)
```

---

## 4. Real modes & safety

Two actions can touch the outside world. Both are **off by default** and gated.

**Jira (real writes).** Set credentials and pass `--real --confirm`:

```bash
export LPA_JIRA_BASE_URL=https://your-site.atlassian.net
export LPA_JIRA_EMAIL=you@example.com
export LPA_JIRA_API_TOKEN=…            # an Atlassian API token
uv run plan jira $GOLDEN --start-date 2026-08-03 --project PMA --real --confirm
```

Real mode creates the epics/stories/links, sets due dates, and **writes each Jira
key back onto the plan** so a re-run **updates instead of duplicating**. Use
`--only id1,id2` for partial approval. Without `--confirm` (or without credentials)
it refuses. The web UI never does this — it only previews and shows the command.

**Status email.** The tooling *renders* the HTML/Markdown update; **sending** it and
running it on a weekly schedule are deploy concerns (RC1-195), not built here.

---

## 5. Configuration

All settings are env-backed with the `LPA_` prefix (see `apps/api/app/config.py`).

| Variable | Default | What |
|---|---|---|
| `LPA_ANTHROPIC_API_KEY` | — | Enables the LLM verbs (breakdown/dependencies/raid). |
| `LPA_ANTHROPIC_MODEL` | `claude-sonnet-5` | Model for the agents. |
| `LPA_DATABASE_URL` | `sqlite:///./launch_planner.db` | The append-only plan store. |
| `LPA_PLAN_PATH` | the flagship golden | Which plan the API renders by default. |
| `LPA_PROJECT_START_DATE` | `2026-08-03` | Default schedule start. |
| `LPA_JIRA_BASE_URL` / `LPA_JIRA_EMAIL` / `LPA_JIRA_API_TOKEN` | — | Real-mode Jira. |
| `LPA_JIRA_PROJECT_KEY` | `PMA` | The scratch project real mode writes to. |

Deploy-only settings: `LPA_PUBLIC_DEMO` (read-only demo: seeds a history on first
boot, enables rate limiting), `LPA_WEB_DIST` (serve the built web same-origin),
`LPA_RATE_LIMIT_PER_MINUTE` (per-IP cap on `/api/*`, demo only).

`GET /api/plan?plan=…&start=YYYY-MM-DD` renders any plan file; `?snapshot=<version>`
renders a committed snapshot.

---

## 7. Deploy

One container (`Dockerfile`): Node builds the web app, Python serves it same-origin
with the API. Run it locally exactly as it runs in production:

```bash
docker build -t launch-planner .
docker run -p 8080:8080 -v lp:/data launch-planner   # → http://localhost:8080
```

The image sets `LPA_PUBLIC_DEMO=true`, so it **seeds** a proposal → commit →
baseline history on first boot (the Baseline/Status/audit views have data), serves
the built UI same-origin (no CORS), and **rate-limits** `/api/*`. The API has no
LLM or write endpoints — agents and real Jira writes are CLI-only — so the public
demo is read-only by construction; `GET /api/info` reports the posture.

**Fly.io** (config in `fly.toml`, with a persistent volume for the SQLite store):

```bash
fly launch --no-deploy                 # create the app (first time)
fly volumes create planner_data --size 1
fly deploy
fly certs add planner.hihelloreid.com  # then CNAME the subdomain to the Fly app
```

---

## 6. Where to go deeper

- **[architecture.md](architecture.md)** — the layering, the ports/adapters, why
  `planner-core` cannot import the agents.
- **[decisions.md](decisions.md)** — the running ADR log (ADR-0001 … 0019): every
  non-trivial choice with its reasoning.
- **[case-study.md](case-study.md)** — the agent's plan vs. the real migration.
- **The flagship fixture** — `fixtures/jira-cloud-migration/`: the PRD, the
  team/constraints, and the hand-reviewed golden plan the whole demo runs on. Its
  `golden/README.md` explains the interesting judgment calls.
