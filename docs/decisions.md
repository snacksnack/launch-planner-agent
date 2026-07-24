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

## ADR-0009 — Dependency Agent: filter edges before they enter the plan; auto-break cycles by weakest edge

**Date:** 2026-07-24 · **Ticket:** RC1-186 (P1.5) · **Status:** Accepted

**Context.** Hallucinated dependencies are the project's highest-risk failure
mode — one bogus edge poisons the critical path. The Dependency Agent proposes
precedence edges and maps gate constraints ("legal sign-off before client data
moves") to edges; deterministic code must guarantee nothing invalid reaches the
plan. Two design points needed settling: the order of filtering vs. stamping,
and how to treat cycles.

**Explanation.** *Two-phase validation.* `ProposedDependency` is deliberately
permissive (no self-loop validator), because a strict `planner_core.Dependency`
would raise at construction and abort the whole decode. Instead the agent runs
`planner_core.filter_dependencies` on the raw proposals **before** stamping:
dangling references, self-loops, and duplicate (predecessor, successor) pairs are
dropped with a reason and returned as `EdgeRejection`s — they never enter a
`Plan`, and only the survivors are converted to canonical `Dependency` objects
with Python-stamped run-facts (same pattern as ADR-0008). *Cycles are broken
automatically, by the weakest edge.* A cycle (`a -> b -> c -> a`) has no valid
schedule, so it cannot be left in the plan — but which edge to cut is a judgment
call. `resolve_cycles` (using `networkx.find_cycle`) iteratively removes the edge
the agent was *least* sure about — lowest provenance confidence, ties broken
deterministically by edge id — until the graph is acyclic. Rationale: the
lowest-confidence edge is the likeliest hallucination, and confidence is exactly
the signal provenance already captures. Every removal is returned as a
`CycleBreak` and surfaced as a prominent **warning** (with the broken cycle's
path) — the plan stays schedulable (`report.ok` is `True`) but a human sees what
was cut and can override. Nothing is dropped silently. The `_cycle_issues` check
in `build_dependency_report` remains as an error-level safety net for any cycle
that reaches the report unresolved (e.g. a hand-authored plan loaded directly).
Orphan tasks, unverifiable quotes, and low-confidence edges are warnings. *Gate
coverage is the
checkable constraint validation at this stage:* a gate whose target task has no
incoming edge is flagged (`unenforced-gate`). The freeze-window *scheduling*
violation the ticket mentions needs computed dates and is deferred to the CPM
engine (P1.6 / RC1-196's blackout-window work). The P1.3 golden (28 real edges)
doubles as the acyclic regression target; AC1's induced cycle is a unit test.

**Consequences.** Structurally-invalid edges are unrepresentable in a committed
plan; a cycle can never survive into the schedule, and the human always sees the
readable path of anything that was cut to break it. If the weakest-edge heuristic
ever cuts the wrong edge, the `CycleBreak` warning is the reviewer's signal to
re-add it and remove a different one. The `plan dependencies plan.json` CLI
resolves the PRD from the plan's
`source_document` so the agent can quote and the validator can verify quotes
verbatim. The live agent spot-check (AC2 end-to-end) needs credentials — run
`plan dependencies fixtures/jira-cloud-migration/plan.json` after `plan
breakdown`.

---

## ADR-0008 — Work Breakdown Agent: LLM proposes reduced provenance, Python stamps run facts

**Date:** 2026-07-23 · **Ticket:** RC1-185 (P1.4) · **Status:** Accepted

**Context.** The first LLM agent turns a messy PRD into a structured WBS (epics,
tasks, owners, three-point estimates) with provenance. Three questions drove the
design: how to force structured output without free-text parsing; who fills the
provenance fields; and where the deterministic post-validation lives given the
`planner_core` ↔ `agents` boundary.

**Explanation.** *Schema-forced, not parsed:* the agent calls
`client.messages.parse(output_format=ProposedWorkBreakdown)` (Anthropic
structured outputs) and gets back a validated Pydantic object — no free-text
extraction. *Reduced proposal:* the model is asked to fill only what it can know
from the document — `reasoning`, verbatim `source_quote`, `source_section`,
`confidence` — via a `ProposedProvenance` shape. The `agent`, `model`, and
`timestamp` fields are facts about the *run*, not the document, so Python stamps
them after parsing (`WorkBreakdownAgent._stamp`). The model literally cannot
hallucinate a timestamp or misattribute the agent. *Validation is deterministic
and lives in the core:* `planner_core.validation` (owners resolve, epics resolve,
low-confidence flags, and — reusing the fixtures trick — a hallucination guard
that flags any `source_quote` not found verbatim in the PRD, plus PRD-section
coverage gaps) is pure and has zero LLM dependency, so it's fully unit-tested
without credentials and sits on the "Python validates" side of the boundary. The
Anthropic client is injectable, so the whole orchestration is tested with a fake.
The `plan breakdown <fixture>` CLI (in `app`) wires it together and prints the
report plus a name-based comparison against the golden baseline.

**Consequences.** Provenance run-facts are trustworthy by construction; the
quote-verbatim check catches the most damaging failure mode (invented
citations). The agent's live spot-check against the golden (the RC1-185
acceptance criterion) needs real credentials — run `plan breakdown
fixtures/jira-cloud-migration/` with `LPA_ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY`)
set. Structured outputs is the chosen forcing mechanism over strict tool-use for
its cleaner round-trip; if a future model/provider lacks it, the agent's
`_propose` step is the single place to swap in forced tool-use.

---

## ADR-0007 — Golden fixtures are self-contained Plans, verified by a loader test

**Date:** 2026-07-23 · **Ticket:** RC1-184 (P1.3) · **Status:** Accepted

**Context.** P1.3 needs the evaluation corpus the whole project demos against: a
messy PRD, a team roster, a constraints file, and a hand-reviewed "golden"
extraction the P1.4/P1.5 agents will be scored against. Two questions had to be
settled: (1) how to relate the input sidecars (`team.json`, `constraints.json`)
to the golden file, and (2) how to keep a hand-authored 24-task/28-dependency
JSON from silently rotting — dangling id references, a stray cycle, or a
provenance quote that doesn't actually appear in the PRD.

**Explanation.** The golden `expected-plan.json` is a **complete, self-contained
`Plan`**: the extracted epics/tasks/dependencies/milestones *plus* the same team
and constraints as the input files. One file loads to the full expected plan,
which is what later tickets want, and a loader test asserts `plan.team` /
`plan.constraints` equal the sidecars so the duplication can't drift. Rather than
trust hand-authoring, `test_fixtures.py` auto-discovers every corpus and enforces
the invariants: model round-trip, all id references resolve, the dependency graph
is acyclic (via `networkx`), and — the important one — every provenance
`source_quote` appears **verbatim** in that corpus's `prd.md` (whitespace-
normalized). Two conventions make the hand-authored provenance honest:
extracted entities use `model="golden-baseline"` (not a real LLM run), and
`confidence` is graded down for genuinely inferred work (rehearsal, closeout,
unnamed tooling). A second, smaller product-launch corpus guards against
overfitting to the migration doc.

**Consequences.** The golden files are trustworthy as a regression baseline: a
typo'd id or a fabricated quote fails CI. The `source_quote`-verbatim test also
back-pressures the model design — provenance is only meaningful if quotes are
real, and now that's machine-checked. One known gap is recorded in the fixtures
README: the P1.2 `Constraint` has a single `hard_date`, so the Q4 freeze
*window* is modeled as a gate; a first-class blackout-window constraint is left
for a later scheduling ticket.

---

## ADR-0006 — Provenance enforced by type, not convention

**Date:** 2026-07-23 · **Ticket:** RC1-183 (P1.2) · **Status:** Accepted

**Context.** The epic's differentiator is that every agent-produced entity
carries an audit trail (`reasoning`, verbatim `source_quote`, `source_section`,
`confidence`, `agent`, `model`, `timestamp`). The acceptance criterion is
strong: *a `Plan` cannot be constructed with an agent-generated entity missing
provenance.* We needed a mechanism that makes that structurally impossible
rather than relying on reviewers to notice a missing block.

**Explanation.** All agent-produced entities (`Epic`, `Task`, `Dependency`,
`Milestone`, `Constraint`) inherit a `ProvenancedModel` base whose sole job is a
required, defaultless `provenance: Provenance` field. Pydantic then rejects any
construction that omits it — including the nested-dict form used when
deserializing a `Plan` — so the guarantee holds end to end. `TeamMember` is the
one deliberate exception: it is human roster input, not an agent extraction, so
it does not inherit the base. Entities reference each other by string id (flat
lists on `Plan`) rather than by nesting, keeping the serialized document
diff-friendly and letting `planner-core` rebuild the graph deterministically in
a later ticket. `plan_json_schema()` publishes `Plan.model_json_schema()` as the
single source of truth agents schema-force against. `extra="forbid"` on every
model keeps schema-forced output honest. Estimates are a `ThreePointEstimate`
value object (ordered `optimistic <= likely <= pessimistic`, exposing PERT
`expected`/`std_dev`) so the CPM engine consumes a computed duration directly.

**Consequences.** Provenance is unforgeable at the type level — the differentiator
can't silently rot. Adding a future agent-produced entity means inheriting
`ProvenancedModel`, which is the intended default. If we ever need a
human-authored task, it must either carry provenance or get its own non-provenanced
base — an explicit decision, not an accident.

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
