# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Python (a `uv` workspace — always run through `uv run`, never a bare `python`/`pytest`):

```bash
uv sync --all-packages        # install every workspace member into one root .venv
uv run ruff check .           # lint (line-length 100, rules E,F,I,UP,B,SIM)
uv run lint-imports           # enforce the architectural boundary — see below
uv run pytest                 # whole suite (testpaths = apps, packages)
uv run pytest packages/planner-core/tests/test_scheduling.py::test_textbook_cpm_early_late_float_and_critical_path
```

Frontend (not in the Python CI matrix; `apps/web` is excluded from ruff):

```bash
cd apps/web && npm install && npm test      # Vitest over apps/web/src/lib.js
```

Run it (two terminals, credential-free — the API serves the flagship golden by default):

```bash
uv run uvicorn app.main:app --reload      # :8000
cd apps/web && npm run dev                # :5173 (VITE_API_BASE overrides the API origin)
uv run python -m app                      # config sanity check, no credentials needed
```

Evals — `uv run evals run <subject>` / `uv run evals report [run-id]` (RC1-248). Exit codes
are CI-shaped: `0` all passed, `1` a case failed, `2` a case errored (the subject produced
nothing to score). Subjects: `health` (free, deterministic) and `tool-selection` (**billed** —
drives a real model over the shipped stdio MCP surface, needs `LPA_ANTHROPIC_API_KEY`, and is
deliberately not part of `uv run pytest` so the suite stays credential-free; ADR-0031).

CLI — `uv run plan <verb>`, from the repo root, paths repo-relative. Deterministic verbs
(`schedule`, `simulate`, `forecast`, `scenario`, `jira`, `propose`, `commit`, `baseline`,
`variance`, `status`, `history`, `show`, `diff`) need no API key. Only `breakdown`,
`dependencies`, and `raid` call the LLM (`LPA_ANTHROPIC_API_KEY`). `docs/HOWTO.md` has a
worked example of every verb.

CI (`.github/workflows/ci.yml`) runs exactly: sync → ruff → lint-imports → pytest.

## Architecture

**LLM proposes, Python validates, human approves.** This is not a slogan — it is the
dependency graph, and it is machine-enforced.

```
evals  ──▶  mcp_server  ──▶  app (apps/api)  ──▶  agents (LLM)  ──▶  planner-core
```

- **`packages/planner-core`** — the deterministic heart: domain models, CPM/critical path
  (`scheduling.py`), simulation, Monte Carlo forecast, diff/baseline, validation, RAID
  analysis, Jira generation-plan building, plan-store models and the `PlanRepository`
  port. Dependencies are exactly pydantic + networkx. **Zero LLM dependencies.**
- **`packages/agents`** — the four LLM agents (work breakdown, dependency, RAID, status).
  Schema-forced structured output (`client.messages.parse`) against `agents/schema.py`
  `Proposed*` models, never free-text parsing. The Anthropic client is injectable, so
  agent orchestration is tested with a fake.
- **`apps/api`** — FastAPI + the `plan` CLI. **Owns all I/O**: the SQLite connection
  (`store.py`, the adapter behind `PlanRepository`), the real Jira HTTP client
  (`jira_client.py`), the Gantt payload builder. `planner_core` does no network or DB I/O.
- **`apps/web`** — vanilla Vite + `frappe-gantt@0.6.1`. The *tested* contract is the
  backend payload (`app/gantt.py`), not the rendering.
- **`apps/evals`** — the quality harness (RC1-230). Frozen `Case`s naming *characteristics*
  rather than expected output, scored per case, appended to a run log carrying subject
  version, token cost, and latency. Top of the layers contract: it may import everything,
  nothing may import it. **Deliberately not a shared library yet** — one consumer means any
  interface is a guess; the extraction is RC1-252. See ADR-0030. Billed subjects drive a
  real model and stay out of the credential-free suite (ADR-0031); prices are a dated local
  snapshot in `evals/pricing.py`, and an unknown model raises rather than costing zero.

### The enforced rule

`planner_core` must never import `agents`, `app`, or `anthropic`. Two import-linter
contracts in the root `pyproject.toml` (a `forbidden` contract and a `layers` contract)
fail CI on a violating edge, plus a unit test asserting `anthropic` isn't reachable
transitively. If you need LLM judgment inside a core module, that is the signal to move
the work to `agents` and pass the result down — not to relax the contract.

### Invariants worth knowing before you edit

- **Provenance is enforced by type.** Every agent-produced entity (`Epic`, `Task`,
  `Dependency`, `Milestone`, `Constraint`) inherits `ProvenancedModel`, whose required
  defaultless `provenance` field makes a provenance-less `Plan` unconstructible.
  `TeamMember` is the one deliberate exception (human roster input). All models are
  `extra="forbid"`. New agent-produced entities inherit `ProvenancedModel`.
- **Python stamps the run facts.** The LLM fills only `reasoning` / `source_quote` /
  `source_section` / `confidence`; `agent`, `model`, and `timestamp` are stamped after
  parsing, so they cannot be hallucinated.
- **Quotes must be verbatim.** `test_fixtures.py` asserts every `source_quote` in every
  fixture appears verbatim (whitespace-normalized) in that corpus's `prd.md`, and
  validation flags any that don't. Editing a fixture's prose can break unrelated tests.
- **The golden has a keyed twin.** `fixtures/jira-cloud-migration/golden/expected-plan.json`
  is the hand-authored ground truth a dozen tests assert against;
  `expected-plan.skyline.json` is the deployed demo's copy carrying SKY Jira keys.
  `apps/api/tests/test_deploy.py` asserts the two are equal once keys are stripped — edit
  the golden and you must propagate to the twin.
- **Snapshots are append-only.** The SQLite plan store installs triggers that `RAISE` on
  UPDATE/DELETE. Saved scenarios live in a separate, deliberately *mutable* `scenarios`
  table — don't conflate the two lifecycles.
- **The API is read-only by construction.** There are no LLM and no plan-of-record write
  endpoints; agents and commits are CLI-only. Saving a scenario is the single mutable HTTP
  surface and is gated off in demo mode. Adding a write or agent endpoint would break the
  deploy posture that `/api/info` advertises and `test_deploy.py` asserts.
- **Forecast randomness stays engine-side and seeded.** A fixed seed reproduces a run
  exactly; `correlation=0` must stay bit-for-bit identical to the pre-copula path (it
  branches to the original sampler because the two consume the RNG stream differently).
- **Simulation never raises on bad input.** Unknown ids, self-loops, cycle-creating edges
  are collected as `warnings` and skipped so an interactive recompute always yields a
  schedule.
- **Reuse the CPM engine, don't fork it.** Simulation, forecast, and baseline variance are
  all thin layers that re-run `compute_cpm` / `schedule_plan` and diff the outputs. There
  should only ever be one implementation of the scheduling math.

## Conventions

- **Every notable technical decision gets an ADR** appended to the top of
  `docs/decisions.md` (newest first) with **Decision / Status / Context / Explanation /
  Consequences** and a `**Date:** … · **Ticket:** RC1-xxx · **Status:**` line. Reversals
  add a new entry and mark the old one `Superseded` — never delete. Read the recent
  entries before changing scheduling, forecast, store, or Jira behaviour; the *why* is
  there and usually rules out the obvious alternative.
- Work is tracked as Jira tickets under epic **RC1-181**; commits and ADRs reference the
  ticket key.
- Config is env-backed via pydantic-settings with the **`LPA_`** prefix
  (`apps/api/app/config.py`, documented in `.env.example`). Credentials are always
  optional — the app and the whole demo must keep booting without them.
- The UI surface count is stated in three places (the on-domain overview page,
  `docs/quickstart.html`, and `docs/HOWTO.md` §2); adding a panel means updating all three.
