# Decision log

A running log of notable technical decisions — what we chose, and *why*, so the
reasoning survives past the moment it was made. Newest entries at the top.
Lightweight [ADR](https://adr.github.io/) style. When a decision is later
reversed, don't delete it — add a new entry that supersedes it and mark the old
one `Superseded`.

Format per entry: **Decision**, **Status**, **Context** (the forces at play),
**Explanation** (why this option won), and **Consequences** (what it commits us
to).

---

## ADR-0005 — Vite (vanilla) for `apps/web`

**Date:** 2026-07-22 · **Ticket:** RC1-182 (P1.1) · **Status:** Accepted (placeholder)

**Context.** The epic calls for `apps/web` to "start minimal — Vite + Gantt lib,
grow into dashboard," served/consumed by the FastAPI app. The real interactive
Gantt is a later ticket (P1.7, RC1-188), which will evaluate `frappe-gantt` vs
`vis-timeline`.

**Explanation.** For P1.1 we only need a placeholder that proves the frontend
build tool is wired up and can talk to the API. A vanilla Vite scaffold (no
React/Vue) is the smallest thing that satisfies that without prejudging the P1.7
UI-framework choice. It pings `/healthz` to demonstrate the API connection.

**Consequences.** No frontend framework committed yet; P1.7 makes that call.
`npm install` is not run in CI (the frontend isn't part of the Python test
matrix yet).

---

## ADR-0004 — Default LLM model `claude-sonnet-5`

**Date:** 2026-07-22 · **Ticket:** RC1-182 (P1.1) · **Status:** Accepted (default, easily changed)

**Context.** The `agents` layer needs a default model. No agent actually calls
the model until P1.4.

**Explanation.** `claude-sonnet-5` is a sensible cost/quality default for
schema-forced structured extraction. It is only a *default* — fully overridable
via the `LPA_ANTHROPIC_MODEL` env var — so committing to it now costs nothing.

**Consequences.** Revisit when P1.4/P1.5 reveal whether the work-breakdown and
dependency agents need a stronger model for quality.

---

## ADR-0003 — Enforce the core/LLM boundary with import-linter in CI

**Date:** 2026-07-22 · **Ticket:** RC1-182 (P1.1) · **Status:** Accepted

**Context.** The epic's flagship architecture rule: `planner-core` (deterministic
scheduling) must have zero LLM dependencies and must not be able to import
`agents`. This is a rule that quietly rots unless something checks it.

**Explanation.** [import-linter](https://import-linter.readthedocs.io/) can fail
CI on a forbidden import edge. We use two contracts: a `forbidden` contract
(`planner_core` may not import `agents`, `app`, or `anthropic`) and a `layers`
contract (`app` → `agents` → `planner_core`). A backup unit test asserts
`anthropic` isn't imported transitively by the core. Verified by injecting a
forbidden import and watching CI go red.

**Consequences.** The boundary is machine-checked from the first commit rather
than relying on discipline. `include_external_packages = true` is required
because `anthropic` is an external module named in the contract.

---

## ADR-0002 — Pin Python 3.12

**Date:** 2026-07-22 · **Ticket:** RC1-182 (P1.1) · **Status:** Accepted

**Context.** The dev machine runs Python 3.14 system-wide; the epic specifies
3.12 for CI.

**Explanation.** We pin 3.12 via `.python-version` and `requires-python`, and let
`uv` fetch/manage that interpreter independently of the system Python. Keeps
local and CI on the same version the epic targets, with the widest library
compatibility.

**Consequences.** `uv` owns the interpreter; contributors don't need 3.12
installed system-wide.

---

## ADR-0001 — `uv`-managed workspace (monorepo of typed Python packages)

**Date:** 2026-07-22 · **Ticket:** RC1-182 (P1.1) · **Status:** Accepted

**Decision.** Manage the repo as a single [`uv`](https://docs.astral.sh/uv/)
**workspace** containing three Python packages — `packages/planner-core`,
`packages/agents`, and `apps/api` — rather than one flat package or a
`pip`/Poetry setup.

**Context.** `uv` is a fast Python package/project manager (from Astral, the
`ruff` authors) that replaces the `pip` + `venv` + `pip-tools`/Poetry stack and
also installs/pins Python itself. A *workspace* is its term for one repo holding
multiple packages developed together — the Python analogue of an npm/pnpm or
Cargo workspace. The epic mandates a CompPilot-style monorepo with a hard
architectural boundary between a deterministic core and an LLM layer.

**Explanation.** The workspace model fits this shape precisely:

1. **One shared venv + one lockfile for all packages.** The root
   `pyproject.toml` lists the members under `[tool.uv.workspace]`; `uv sync
   --all-packages` resolves every package's dependencies together into a single
   root `.venv/`, frozen into one `uv.lock`. A fresh clone gets byte-identical
   versions — exactly what the "passes on a fresh clone" acceptance criterion
   needs.
2. **Local packages resolve to each other by source, not from PyPI.**
   `[tool.uv.sources]` marks `planner-core`/`agents` as `workspace = true`, so
   `agents` → `planner-core` and `app` → both are edit-in-place across package
   boundaries — no reinstall, no publishing.
3. **`uv run` executes inside that shared venv.** `uv run pytest` / `ruff` /
   `lint-imports` all run against the one environment with all three packages
   importable, no manual activation.

Crucially, making the core and LLM layer *genuinely separate installable
packages* (rather than folders in one package) is what gives import-linter a
real dependency edge to police (see ADR-0003). The workspace is what makes
"three separate packages" ergonomic to develop as one repo.

**Consequences.** Contributors need `uv` (installed here at `~/.local/bin`, not
on the default PATH — commands are prefixed with
`export PATH="$HOME/.local/bin:$PATH"`). Reversible at this stage if we ever
want a flat single-package layout or a different tool.
