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
  web/            # Gantt UI (Vite; grows into the dashboard in P1.7)
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

### Checks

```bash
uv run ruff check .           # lint
uv run lint-imports           # enforce planner-core has no LLM/app deps
uv run pytest                 # tests across all packages
```

## Status

Phase 1 (demoable core) in progress — see epic **RC1-181**. This is the
scaffold (**P1.1**); the domain model, agents, scheduling engine, and Gantt UI
follow.
