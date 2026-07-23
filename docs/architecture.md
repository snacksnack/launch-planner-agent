# Architecture

> The *why* behind these choices lives in the running [decision log](decisions.md).

## Guiding principle

**LLM proposes, Python validates, human approves.**

| Concern | Owner | Why |
| --- | --- | --- |
| Work breakdown, dependency inference, RAID, status narrative | `agents` (LLM) | Requires judgment over messy prose |
| CPM / critical path, float, slippage, cycle detection, constraint checks | `planner-core` (deterministic) | Must be correct, inspectable, and testable without a model |
| Commit to plan-of-record, write to real Jira | Human approval gate | Nothing irreversible happens autonomously |

## Package layout & dependency direction

```text
app  ──▶  agents  ──▶  planner-core
(apps/api)          (LLM layer)      (deterministic core)
```

- **`planner-core`** — the deterministic heart. Task graph, dependency model,
  CPM/critical-path scheduling, validation, and the plan-store models
  (snapshots, baselines, audit trail). **Zero LLM dependencies.**
- **`agents`** — the LLM judgment layer. May import `planner-core`; produces
  schema-forced structured output conforming to its models.
- **`app` (apps/api)** — FastAPI. Ingestion, agent orchestration, persistence.
  **Owns the actual database connection**; the plan-store *models* live in
  `planner-core`.
- **`apps/web`** — the Gantt UI (Vite), served/consumed by the API.

### The enforced rule

`planner-core` must **never** import `agents`, `app`, or `anthropic`. The
deterministic engine cannot even reference LLM code. This is enforced two ways:

1. **CI** runs [import-linter](https://import-linter.readthedocs.io/)
   (`uv run lint-imports`) with a `forbidden` contract on `planner_core` and a
   `layers` contract (`app` → `agents` → `planner_core`). A violating import
   fails the build.
2. A unit test in `planner-core` asserts `anthropic` is not imported
   transitively.

Retrofitting this direction later is painful, so it is enforced from the first
commit.

## Provenance is first-class (from day one)

Every agent-produced entity (task, dependency, risk) embeds a provenance block:
`reasoning`, verbatim `source_quote`, `source_section`, `confidence`, `agent`,
`model`, `timestamp`. This is the project's key differentiator — the plan is an
audit trail, not a black box. The models land in **P1.2**.

## Portfolio positioning

The planner creates the plan at planning time; the **Dependency Drift Detector**
(tpm-automation-platform) watches it at runtime; the **Status Agent** reports on
it.
