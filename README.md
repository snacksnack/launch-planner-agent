# launch-planner-agent

An agentic planning tool for **migrations and launches**. It takes a PRD /
technical spec, a team list, milestones, and constraints, and produces a full
delivery plan: an interactive Gantt chart with critical path, a slippage
simulator, a RAID log, generated Jira tickets (behind an approval gate), and
weekly exec status updates.

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
  web/            # Gantt UI + dashboard (Vite): decisions, RAID, simulate, baseline, status, Jira
  api/            # FastAPI: ingestion, agent orchestration, persistence
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
CLI verb. It all runs credential-free against the flagship golden plan. There's
also a one-page [visual quick-start](https://claude.ai/code/artifact/89cfe001-3652-4ffd-85f1-815836d83031).

### Checks

```bash
uv run ruff check .           # lint
uv run lint-imports           # enforce planner-core has no LLM/app deps
uv run pytest                 # tests across all packages
```

## Status

Phases 1–3 built (epic **RC1-181**): the domain model + provenance, the four
agents (work breakdown, dependency, RAID, status), the deterministic CPM engine,
the interactive Gantt with a decisions/RAID/simulate/baseline/status/Jira
dashboard, the event-sourced plan store with baselines, and gated Jira ticket
generation. Remaining: deploy + audit-trail viewer & demo polish (**RC1-195**).
See the [HOWTO](docs/HOWTO.md) to run it and the [decision log](docs/decisions.md)
for the reasoning.
