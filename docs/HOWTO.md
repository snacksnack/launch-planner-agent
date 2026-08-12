# HOWTO — using launch-planner-agent

A task-oriented guide: how to **run it**, **use every CLI verb**, and **read every
UI surface**. For *why* it's built the way it is, see
[architecture.md](architecture.md) and the [decision log](decisions.md); for the
"agent's plan vs. what actually happened" story, see [case-study.md](case-study.md).

Everything here runs **credential-free** against the flagship golden plan
(`fixtures/jira-cloud-migration/`) unless a step is marked **needs an API key**.

> **Prefer a visual tour?** The [project overview
> page](https://www.hihelloreid.com/projects/launch-planner) walks the pipeline and
> the eight dashboard surfaces, and links to the [live
> demo](https://planner.hihelloreid.com). There's also a one-page [visual
> quick-start](https://claude.ai/code/artifact/89cfe001-3652-4ffd-85f1-815836d83031)
> that adds the two-terminal local setup (source: `docs/quickstart.html`; private by
> default — shareable from the page's share menu).

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

There is a third surface beyond the API and the web UI: an **MCP server**, so a
client like Claude Desktop can drive the planner in conversation. It needs no
credentials either — see [§5](#5-the-mcp-server--the-planner-in-conversation).

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

**Save & recall (RC1-202).** Once a scenario is composed, name it and **Save
scenario**; it's kept beside the plan (scoped to the plan's content hash). Saved
scenarios list under the panel with each one's launch impact (e.g. `+24d`) — a
side-by-side comparison — and one click **re-applies** a saved scenario, reproducing
the identical schedule delta. The **×** deletes one. (Saving is a local convenience:
it's disabled in the read-only public demo; the list still shows.)

### Forecast — *the launch date as a probability, not a point*
The **Forecast** button runs a **Monte Carlo** over the three-point estimates:
each task's duration is sampled from a Beta-PERT distribution and CPM is re-run
1,000 times. The panel gives a **confidence band** — "80% chance of launching on
or before <date>" (P50/P80/P90) — over a finish-date histogram with the P50/P80/P90
and the deterministic point estimate marked. Below it, the **criticality index**
ranks each task by how often it landed on the critical path across the runs (the
true, risk-weighted schedule drivers). It's deterministic for a fixed seed — no
randomness reaches the browser.

*Example (flagship golden):* the deterministic plan lands **Oct 12**, but only
~19% of runs actually hit it; 80% confidence is **Oct 23**.

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

In the [live demo](https://planner.hihelloreid.com) the previewed issues are
**clickable and real**: a one-off gated run pushed this plan to a public Skyline
(`SKY`) project, so each one opens the actual ticket — provenance audit and all —
for an anonymous visitor. Issues with no key fall back to jumping to the task.

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

# Monte Carlo the launch date over the three-point estimates (P50/P80/P90 + criticality)
uv run plan forecast $GOLDEN --start-date 2026-08-03 --seed 42
uv run plan forecast $GOLDEN --start-date 2026-08-03 --seed 42 --correlation 0.4  # common-cause risk

# Save / list / load / delete named what-if scenarios (persisted beside the plan)
uv run plan scenario save $GOLDEN --name "legal blows up" --slip task-legal-review:30 --by Priya
uv run plan scenario list $GOLDEN --start-date 2026-08-03            # each with its launch impact
uv run plan scenario load $GOLDEN --name "legal blows up" --start-date 2026-08-03   # reproduce the delta
uv run plan scenario delete $GOLDEN --name "legal blows up"

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

## 5. The MCP server — the planner in conversation

The same engines, driven by talking to them. An MCP client spawns
`launch-planner-mcp` over stdio and gets nine tools; a question like *"what if
the legal sign-off slips a month?"* becomes a real number out of the real CPM
engine rather than a guess.

Nothing here writes. See [§4](#4-real-modes--safety) for the boundary and how it
is enforced; the [README](../README.md#mcp-server--the-planner-as-conversational-tools)
has the design rationale and [ADR-0027](decisions.md) the reasoning behind it.

### Connect a client

Add this to your client's config — for Claude Desktop that is
`claude_desktop_config.json` — and restart it:

```json
{
  "mcpServers": {
    "launch-planner": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/launch-planner-agent",
               "launch-planner-mcp"],
      "env": { "LPA_DRIFT_BASE_URL": "" }
    }
  }
}
```

The path must be absolute — the client does not inherit your shell's working
directory. `LPA_DRIFT_BASE_URL` is optional; see *Drift* below.

To check the server itself before involving a client:

```bash
uv run launch-planner-mcp        # prints a readiness line on stderr, then waits
```

It will sit there silently waiting for JSON-RPC on stdin, which is correct — a
client is what talks to it. Ctrl-C to exit.

### What works immediately, and what needs a step

**Clone the repo, paste the config, and six of the nine tools answer real
questions with no further setup.** They read the flagship golden plan file
(`LPA_PLAN_PATH`), not the database, so nothing has to be seeded or committed
first.

| Tool | Ready on a fresh clone? |
| --- | --- |
| `platform.health` | yes |
| `plan.list` | yes |
| `plan.get` | yes |
| `plan.critical_path` | yes |
| `plan.simulate` | yes |
| `plan.forecast` | yes |
| `status.draft` | needs a committed baseline |
| `drift.check` | needs a drift service |
| `drift.explain` | needs a drift service |

**`status.draft`** compares the current plan against a committed baseline, so it
needs one to exist. One command:

```bash
uv run plan baseline fixtures/jira-cloud-migration/golden/expected-plan.json \
  --by "Priya Nair" --note "initial plan"
```

Until then it fails with an explanation rather than returning an empty update —
"nothing to compare against" and "nothing changed" mean opposite things, and a
model handed an empty result would report the second.

**Drift** needs `LPA_DRIFT_BASE_URL` pointing at a deployed
[tpm-automation-platform](https://github.com/snacksnack/tpm-automation-platform)
with the read-only findings endpoints. Left unset, `drift.check` and
`drift.explain` report unavailable and every other tool carries on — that is a
configuration state, not a fault. `platform.health` tells you which it is.

**Nothing here creates a database.** All nine tools treat a missing plan store as
"no snapshots", because opening one would run its migration and create the file —
a write, from a server whose whole claim is that it does not write.

### The nine tools

Grouped by the question they answer.

**Orientation — what am I looking at?**

| Tool | Answers | Key arguments |
| --- | --- | --- |
| `platform.health` | Is the plan store readable? Is drift answering? | none |
| `plan.list` | Which plans exist — every snapshot, plus the default | none |
| `plan.get` | When does this launch, how big is it, where are the milestones? | `ref`, `start`, `detail` |

`plan.list` is the entry point when you have no plan reference. Every response
from every plan tool echoes a `canonical_ref` you can pass straight back as
`ref`, so there are no ID formats to guess.

**Analysis — why is the date what it is?**

| Tool | Answers | Key arguments |
| --- | --- | --- |
| `plan.critical_path` | Which chains drive the date, who owns them, how much float | `ref`, `start`, `include_near_critical` |
| `plan.simulate` | What if a task slips N working days? | `task`, `days`, `ref`, `start` |
| `plan.forecast` | How confident are we — P50/P80/P90, and what is most likely to delay us | `ref`, `start`, `iterations`, `seed` |

These three are easy to confuse, so:

- `plan.critical_path` is **one deterministic pass** over the most-likely
  estimates. It returns *the* critical chains — plural, because a schedule can
  have several converging ones (the flagship golden has two).
- `plan.forecast` **samples** those estimates a thousand times and reports a
  probability band, plus a *criticality index* — how often each task landed on a
  critical path. "What drives the date" is the first tool; "how likely is that
  date" is the second.
- `plan.simulate` applies one hypothetical slip and re-runs the engine. Read its
  `outcome` field before reporting anything: a slip smaller than the task's float
  is **absorbed** and the date holds, which is a real finding and not the same as
  the change having been rejected.

**Reporting and runtime**

| Tool | Answers | Key arguments |
| --- | --- | --- |
| `status.draft` | The weekly exec update against the baseline | `current`, `baseline`, `start`, `period` |
| `drift.check` | What the drift detector last found | `bucket`, `rule`, `since_run` |
| `drift.explain` | Why one finding fired | `finding_id` |

`status.draft` drafts and never sends. `drift.check` reports the **last scheduled
run**, not a live scan — every response carries `run_id`, `run_at` and
`is_live: false`. Pass a `finding_id` from `drift.check` straight to
`drift.explain`; do not compose one.

Plan references (`ref`, `current`, `baseline`) all accept the same forms: a
version number, a content-hash prefix of four or more characters, `latest`,
`baseline`, or omitted for the default plan.

### Troubleshooting

**The client shows no tools, or fails to connect.**
Run `uv run launch-planner-mcp` yourself from the repo root. If that prints a
readiness line and waits, the server is fine and the problem is the client's
config — most often a relative `--directory` path, or `uv` not being on the PATH
the client launches with (GUI apps do not always inherit your shell's).

**Where the server's output goes.**
The client owns the process, so you will not see its output in a terminal.
**stdout carries the protocol** — anything printed there corrupts the stream and
breaks every client. Diagnostics go to stderr, which your client surfaces in its
MCP or developer logs. This is guarded by a test that spawns the real subprocess
(`apps/mcp/tests/test_stdio.py`), because a stray `print` anywhere in the import
graph would break clients while every in-process test stayed green.

**A tool returned an error.** They are structured, and the code says what to do:

| Code | Meaning | Do |
| --- | --- | --- |
| `plan_not_found` | The plan reference matched nothing | Call `plan.list`; the message lists what exists |
| `ambiguous_plan_ref` | A hash prefix matched several snapshots | Use more characters, or the version number |
| `task_not_found` | No task matched that name or id | The message suggests near misses; `plan.critical_path` lists real names |
| `ambiguous_task_ref` | A task name matched several tasks | Name one exactly, or pass its task id |
| `invalid_argument` | An argument was out of range or malformed | The message states the accepted range |
| `drift_unavailable` | The drift service is unset or unreachable | Set `LPA_DRIFT_BASE_URL`, or accept that drift tools are off |
| `internal_error` | A bug | The message names the exception; the rest of the server is unaffected |

**A number does not match the dashboard.** Check the `ref` in the response — a
plan of record moves, and a tool answering about version 3 while the dashboard
renders version 5 is not a disagreement. Forecasts additionally vary by `seed`,
which is echoed on every response for exactly this reason.

**Changed the code and the client did not notice.** The client spawns the process
once; restart the client, or disconnect and reconnect the server.


---

## 6. Configuration

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
the built UI same-origin (no CORS), and **rate-limits** `/api/*`. The API exposes no
LLM, plan-of-record, or Jira writes — agents and commits are CLI-only — and the one
mutable surface (the saved-scenario scratchpad) is **also disabled** in the demo, so
it's read-only by construction; `GET /api/info` reports the posture
(`scenario_writes: false`).

**Fly.io** (config in `fly.toml`, with a persistent volume for the SQLite store):

```bash
fly launch --no-deploy                 # create the app (first time)
fly volumes create planner_data --size 1
fly deploy
fly certs add planner.hihelloreid.com  # then CNAME the subdomain to the Fly app
```

### Backing up the plan of record

The store enforces immutability with triggers, which protects the audit trail
from tampering and not at all from loss — it is one SQLite file on one volume.

```bash
uv run plan backup            # take one, verify it, prune old ones
uv run plan backup --list     # what exists, newest last
```

`plan backup` uses `VACUUM INTO`, so it is safe while the service is running.
It is **not** a file copy: since [ADR-0028](decisions.md) the store runs in WAL
mode, and copying the `.db` alone would miss commits still sitting in the `-wal`
sidecar. The backup is opened and read before it is stored, so a corrupt copy
never replaces a good one.

Configure the destination:

```bash
LPA_BACKUP_S3_BUCKET=my-bucket          # unset -> a local directory, which is
LPA_BACKUP_S3_ENDPOINT_URL=https://...  # not a backup in production
LPA_BACKUP_S3_PREFIX=plan-store/
LPA_BACKUP_KEEP=14                      # newest N retained
```

S3 support needs the extra: `uv sync --all-packages --extra s3`. On Fly, Tigris
is the low-friction option (`fly storage create`), which sets the AWS-style
credentials in the app's secrets.

Run it daily from the same scheduler that drives the drift detector. See
[ADR-0029](decisions.md) for why daily rather than per-commit.

### Restoring

Restore never writes in place — it fetches to a path you name and verifies the
file opens as a plan store, reporting the snapshot count.

```bash
uv run plan backup --list
uv run plan backup --restore launch-planner-20260812T150912Z.db --into ./restored.db

# Inspect before trusting it
LPA_DATABASE_URL=sqlite:///./restored.db uv run plan history
```

Swapping it in is a deliberate step with the service stopped:

```bash
fly scale count 0                                  # stop writers first
fly ssh console -C "mv /data/launch_planner.db /data/launch_planner.db.bak"
# copy the restored file to /data/launch_planner.db, then
fly scale count 1
```

Keep the displaced database as `.bak` until the restored one has served real
traffic. A restore procedure nobody has executed is a hypothesis — this one has
been run end to end against a seeded store, and the restored copy's versions,
content hashes, and approvers were verified identical to the source.

---

## 8. Where to go deeper

- **[architecture.md](architecture.md)** — the layering, the ports/adapters, why
  `planner-core` cannot import the agents.
- **[forecasting.md](forecasting.md)** — the Monte Carlo launch forecast in full: the
  Beta-PERT sampler, correlated durations, the merge-bias theory, determinism, and the
  assumptions.
- **[decisions.md](decisions.md)** — the running ADR log: every non-trivial choice
  with its reasoning.
- **[mcp-demo.md](mcp-demo.md)** — a five-minute conversational demo script for the
  MCP server: the questions, the tool each should route to, and the numbers to expect.
- **[case-study.md](case-study.md)** — the agent's plan vs. the real migration.
- **The flagship fixture** — `fixtures/jira-cloud-migration/`: the PRD, the
  team/constraints, and the hand-reviewed golden plan the whole demo runs on. Its
  `golden/README.md` explains the interesting judgment calls.
