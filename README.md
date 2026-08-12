# launch-planner-agent

An agentic planning tool for **migrations and launches**. It takes a PRD /
technical spec, a team list, milestones, and constraints, and produces a full
delivery plan: an interactive Gantt chart with critical path, a slippage
simulator (with saveable what-if scenarios), a Monte Carlo launch-date forecast,
a RAID log, generated Jira tickets (behind an approval gate), and weekly exec
status updates.

## Core principle

> **LLM proposes, Python validates, human approves.**

Work breakdown, dependency inference, RAID, and narrative are LLM judgment
(schema-forced structured output). Critical-path math, float, slippage
simulation, cycle detection, and constraint checks are **deterministic Python**.
Nothing writes to real Jira without an explicit human approval step.

Every agent output carries an **audit trail** — reasoning, a verbatim source
quote from the input doc, and a confidence level — so the plan is inspectable,
not a black box.

## Layout

```text
apps/
  web/            # Gantt UI + dashboard (Vite): decisions, RAID, simulate, forecast, baseline, status, Jira
  api/            # FastAPI: ingestion, agent orchestration, persistence
  mcp/            # MCP server (package `mcp_server`): the planner as conversational tools — read-only
packages/
  planner-core/   # task graph, CPM/critical path, validation, plan-store models — ZERO LLM deps
  agents/         # work breakdown, dependency, RAID, status agents (LLM)
fixtures/         # sample PRDs + team/constraints files
docs/
  architecture.md
  case-study.md   # agent's plan vs. what actually happened on the real migration
```

`planner-core` cannot import `agents` — the deterministic engine cannot even
reference LLM code. This import direction is enforced in CI. See
[docs/architecture.md](docs/architecture.md) and the running
[decision log](docs/decisions.md) for why things are the way they are.

## Architecture — agents vs. the engine

```text
  PRD ─▶  ┌─────────────── agents (LLM, schema-forced) ───────────────┐
          │  Work Breakdown → Dependency → RAID → Status              │
          │  each output: reasoning + verbatim quote + confidence     │
          └──────────────────────────┬───────────────────────────────┘
                     proposes         │        (never decides the math)
                                      ▼
          ┌──────────── planner-core (deterministic Python) ──────────┐
          │  CPM / float / critical path · cycle detection · schedule │
          │  diff · simulation · validation · event-sourced store     │
          └──────────────────────────┬───────────────────────────────┘
                     validates        │        (cannot import the agents)
                                      ▼
                          human reviews & commits  ─▶  Gantt · RAID · Simulate
                                                        Forecast · Baseline · Status · Jira
```

**Provenance is the spine.** Every agent-produced entity — epic, task, dependency,
milestone, RAID item — carries a mandatory provenance block: *why* it was proposed,
the *verbatim quote* from the PRD that justifies it, *which agent/model* produced it
*when*, and a *confidence*. A `Plan` literally cannot be constructed with an
agent-generated entity that lacks provenance. That's what makes the plan an audit
trail: the **"How this plan was made"** view reconstructs the whole chain — agents
proposed → Python validated (what it dropped, cut, or flagged) → human approved.

**Why it's hard.** Anyone can ask an LLM to draft a project plan. The difficulty is
making it *trustworthy*: the critical-path math and the approval gates are
deterministic and provable (hallucinated dependencies can't reach the schedule),
the audit trail is first-class rather than bolted on, and side effects (Jira writes,
status emails) are gated behind explicit human approval. The LLM proposes; it never
gets to decide whether the launch date is real.

**Positioning.** One of three delivery-intelligence tools: the **planner creates**
the plan → a **drift detector watches** it → the **status agent reports** on it.

## Launch forecast — a date you can put a number on

The deterministic schedule reports one launch date from each task's *most-likely*
estimate. The **Forecast** turns that into a probability. Each task carries a
three-point estimate (optimistic / likely / pessimistic); the forecast samples every
task's duration from a **Beta-PERT** distribution and re-runs the critical-path
engine **1,000 times**, then reports the launch date as a distribution: a P50 / P80 /
P90 confidence band ("80% chance of launching on or before *Oct 23*") plus a per-task
**criticality index** — how often each task landed on the critical path across the
runs, i.e. the true risk-weighted schedule drivers.

Two properties make it defensible: it's **deterministic** (a seeded RNG, so a fixed
seed reproduces a run exactly and *no randomness reaches the frontend*), and it
reveals a **structural optimism bias** that a single CPM pass can't — because the
finish is a max over converging paths, the expected date is provably *later* than the
single-point date ([Jensen's inequality](docs/forecasting.md#6-why-the-single-point-date-is-biased-not-just-uncertain)).
On the flagship golden the deterministic plan lands Oct 12, but only ~19% of runs
actually hit it.

→ **Full explanation:** [docs/forecasting.md](docs/forecasting.md) (the math, the
Beta-PERT sampler, the merge-bias theory, and the assumptions). Run it with
`uv run plan forecast <plan> --start-date … --seed …`.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv will fetch it).

```bash
uv sync --all-packages        # install the whole workspace into one venv
cp .env.example .env          # optional; the app boots without credentials

uv run python -m app          # config sanity check (no credentials required)
uv run uvicorn app.main:app --reload   # serve the API (GET /healthz)
```

Then run the UI and open the demo — **see the [HOWTO](docs/HOWTO.md)** for the
two-terminal quick start, a tour of every UI panel, and worked examples for every
CLI verb. It all runs credential-free against the flagship golden plan. For a
visual tour, see the [project overview
page](https://www.hihelloreid.com/projects/launch-planner) and the [live
demo](https://planner.hihelloreid.com); the one-page [visual
quick-start](https://claude.ai/code/artifact/89cfe001-3652-4ffd-85f1-815836d83031)
(source: `docs/quickstart.html`) adds the local two-terminal setup.

### Checks

```bash
uv run ruff check .           # lint
uv run lint-imports           # enforce planner-core has no LLM/app deps
uv run pytest                 # Python tests across all packages
(cd apps/web && npm test)     # frontend unit tests (Vitest) for the UI's pure logic
```

## MCP server — the planner as conversational tools

The planner is also an [MCP](https://modelcontextprotocol.io/) server, so any MCP
client can drive it in conversation: *"what's the P80 launch date if the auth work
slips a week?"* → tool call → a real number out of the real CPM engine.

```bash
uv run launch-planner-mcp            # stdio; a client spawns this, you rarely run it by hand
```

Client config (Claude Desktop, or any stdio MCP client):

```json
{
  "mcpServers": {
    "launch-planner": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/launch-planner-agent", "launch-planner-mcp"],
      "env": { "LPA_DRIFT_BASE_URL": "" }
    }
  }
}
```

`LPA_DRIFT_BASE_URL` is optional — leave it unset and the drift tools report
unavailable while every planner tool keeps working.

### Tools

| Tool | What it answers |
| --- | --- |
| `platform.health` | Is the plan store readable? Is the drift service answering? |
| `plan.list` | Which plans exist — every snapshot, plus the default used when no ref is given |
| `plan.get` | When does this plan launch, how long is it, where are the milestones? |
| `plan.critical_path` | What is driving the date — every critical chain, with owners and float |
| `plan.simulate` | What if a task slips N working days — the new launch date and what moved |
| `plan.forecast` | The launch date as a P50/P80/P90 band, plus the criticality index |
| `status.draft` | The weekly exec update against the committed baseline — drafted, never sent |
| `drift.check` | What the drift detector last found — findings by severity, with evidence |
| `drift.explain` | Why one finding fired: the tickets, the dates, the change that triggered it |

### Read-only, enforced rather than asserted

No tool writes to the plan store, writes to Jira, calls an LLM, or sends
anything. `plan.simulate` takes a what-if scenario but applies it to an in-memory
copy and persists nothing.

Elsewhere in this repo that property is *structural* — the deployed API simply has
no write endpoints, so `plan commit` and `plan jira` are unreachable from the
served surface. The MCP process runs inside the repo and could import those paths,
so here it is enforced by three mechanisms that each cover something the others
cannot:

- an **import contract** (`mcp_server is read-only`) stops the package reaching
  `app.cli`, `agents`, or `anthropic` at all;
- an **allowlist** (`mcp_server.allowlist`) stops a tool being exposed without
  someone consciously listing it;
- a **call-level sweep** (`tests/test_read_only_sweep.py`) exercises all nine
  tools with the store, Jira, and Anthropic write paths patched to fail. This is
  the one the others miss: `SQLiteEventStore` is legitimately imported for reads,
  and `append` / `save_scenario` / `delete_scenario` hang off the same class.

Each was verified by injecting its violation and watching it fail. See
[ADR-0027](docs/decisions.md#adr-0027--the-mcp-server-lives-in-the-repo-and-trades-a-structural-guarantee-for-an-enforced-one).

### How answers are grounded

**Every response says which plan it read.** Tools take a friendly `ref` — a
version number, a content-hash prefix of 4+ characters, `latest`, `baseline`, or
omitted for the default plan — and echo back a `canonical_ref` that resolves to
exactly that plan. An ambiguous hash prefix is an error listing the candidates
rather than a silent pick. File paths are deliberately not accepted from a
caller; the default is set by `LPA_PLAN_PATH`, an operator's decision rather than
a model's.

**A zero is not always good news.** `plan.simulate` returns an explicit
`outcome`: a slip smaller than the task's float is absorbed and the launch date
holds (`absorbed_by_float`), while a rejected change also leaves the date unmoved
(`not_applied`) and means the opposite. The engine deliberately never raises on
bad input, so task references resolve to a real id *before* the simulation runs.

**The plan's own date comes with its odds.** `plan.forecast` reports the
deterministic single-point date alongside `deterministic_confidence` — the share
of sampled runs that achieved it. On the flagship golden that is Oct 12 at 19%,
against a P80 of Oct 23. Given both figures unlabelled, a model reports the
earlier one.

**Critical chains are plural.** A schedule can have several converging critical
paths — the golden has two. `plan.critical_path` returns all of them with a
count, ordered along their own dependency edges, in one deterministic CPM pass;
`plan.forecast` answers the different question of how *often* each task is
critical across sampled runs.

**Drift is read, never scanned.** The drift tools call read-only endpoints on
`tpm-automation-platform` — the only network calls in this server. A scan
(`POST /drift/run`) collects from Jira, calls an LLM, and DMs owners on Slack, so
it is unreachable from any tool here. Responses carry the run they came from and
`is_live: false`: these are the last *scheduled* run's findings.

**Response sizes are bounded.** `plan.get` returns a summary by default — 1.4 KB
on the golden, against 41.7 KB for the full Gantt payload behind `detail=true`
(most of the difference is per-task provenance carrying verbatim PRD quotes).
`plan.forecast` omits the finish-date histogram; it is a chart, not an answer.
Both are pinned by tests.

### Demo

[docs/mcp-demo.md](docs/mcp-demo.md) is a five-minute conversational script —
the questions, the tool each should route to, and the numbers to expect. It
doubles as the discoverability check: the questions are phrased the way a person
would ask them, and a wrong tool pick means a description needs fixing.

### Honest gaps

- **No audience shaping** on `status.draft`. The service has no audience concept,
  and adding one in the MCP layer would put content logic in a transport wrapper.
- **Drift data is the last scheduled run**, not live. On-demand rescanning is a
  write and stays behind the platform's approval gate.
- **`correlation` is not exposed** on `plan.forecast`. It defaults to 0, matching
  the dashboard; ADR-0026 kept it out of the UI because the units are
  unestimable, and a model is likelier than a human to set a plausible-sounding
  value. It is echoed in the response so a stored forecast records how it ran.
- **`narrative_source` is always `deterministic`.** The LLM narrative comes from
  the gated `plan status` CLI, which the import contract stops this server from
  reaching.
- **Write tools are out of scope for v1.** A later version could add them, but
  only routed through the same human approval gates the CLI uses.

## Deploy

A single container (`Dockerfile`): Node builds the web app, Python serves it
same-origin with the API — no separate web server, no CORS in production. The
public demo is **read-only by construction** (the API has no LLM or write
endpoints; agents and commits are CLI-only), seeds a proposal → commit → baseline
history on first boot (`LPA_PUBLIC_DEMO`), and rate-limits the compute endpoints.

```bash
docker build -t launch-planner . && docker run -p 8080:8080 -v lp:/data launch-planner
# → http://localhost:8080  (full UI + API, seeded, one service)
```

Fly.io config is in `fly.toml` (persistent volume for the SQLite store, `/healthz`
check). See the **[Deploy section of the HOWTO](docs/HOWTO.md#7-deploy)** for the
`fly launch` / `fly deploy` / custom-domain steps.

## Status

Phases 1–3 built (epic **RC1-181**): the domain model + provenance, the four
agents (work breakdown, dependency, RAID, status), the deterministic CPM engine,
the interactive Gantt with a decisions/RAID/simulate/forecast/baseline/status/Jira
dashboard, the event-sourced plan store with baselines, and gated Jira ticket
generation. Remaining: deploy + audit-trail viewer & demo polish (**RC1-195**).
See the [HOWTO](docs/HOWTO.md) to run it and the [decision log](docs/decisions.md)
for the reasoning.
