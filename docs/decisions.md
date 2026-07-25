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

## ADR-0014 — Decision record: a durable build-time audit kept beside the plan, not inside it

**Date:** 2026-07-25 · **Ticket:** RC1-197 · **Status:** Accepted

**Context.** The project's differentiator is that a plan is an *audit trail, not a
black box* — but that only held for provenance (which was already on every entity
and surfaced in the UI). The *decisions* the pipeline made — edges the filter
rejected, edges cut to break a cycle, low-confidence extractions, unverifiable
quotes, unenforced gates, uncited PRD sections — lived only in ephemeral CLI
stdout. Rejections and cycle-breaks in particular leave **no trace in
`plan.json`**: once the losing edge is dropped, it's gone. A reviewer couldn't see
any of it without reading logs.

**Explanation.** *Two kinds of fact, one record.* A `DecisionRecord`
(`planner_core`) holds both the **non-recomputable** run-time facts
(`rejected_edges`, `cycle_breaks` — captured by the Dependency Agent as the graph
is built, then gone) and the **recomputable** deterministic flags
(`flagged`, `coverage_gaps` — pure functions of `plan + PRD`).
`build_decision_record` assembles the whole thing from the two existing report
objects, so there's one definition of "a decision". *Beside the plan, not inside
it.* The record is metadata about *how* the plan was built, not part of the plan,
so embedding it in `Plan` would pollute the content hash and every diff. Instead
it rides a **`decisions.json` sidecar** (written by `plan dependencies`, kept at
`plan.decisions.json`) and is frozen onto the immutable `Snapshot` at commit time
(new nullable `decision_json` column, added by an append-only-safe `ADD COLUMN`
migration) — so the audit is as durable as the plan of record. *Recompute when
there's no run.* The API serves the persisted record for a committed snapshot, but
for a raw plan file (the credential-free golden, never run through the agent) it
**recomputes** the recomputable half from plan + resolved PRD; without the PRD the
source-dependent checks are suppressed rather than emitted as false positives.
*Provenance was already there,* so the UI work is a lean **Decisions panel** (drop
counts, cut edges, flags that link to the entity) plus a subtle amber low-confidence
flag in the task column — the "honest gaps" cue, not an alarm.

**Consequences.** Both acceptance criteria hold: a reviewer sees the
low-confidence items, dropped/cut edges and reasons, and can trace any entity to
its PRD quote — in the UI, not logs; and cycle-breaks/rejections are persisted on
the immutable snapshot. The golden demo shows low-confidence + coverage-gaps
(dropped edges are zero because it's hand-authored clean — those appear on a real
agent run). The snapshot schema now carries build-time metadata; a Postgres
adapter must add the same nullable column behind the port.

---

## ADR-0013 — Milestones are dependency-graph nodes, linked by task → milestone edges

**Date:** 2026-07-25 · **Ticket:** RC1-198 · **Status:** Accepted

**Context.** The CPM engine already treats milestones as zero-duration nodes and
can project a date + slack-to-target for any milestone — but only once a
dependency edge actually reaches it. The Dependency Agent proposed edges between
*tasks* only, and `filter_dependencies` rejected any endpoint that wasn't a task
id, so the flagship milestones stayed unlinked and unprojected, leaving RC1-187's
"projected date + slack per milestone" criterion only partly demonstrable.

**Explanation.** *Reuse the edge, don't add a concept.* A milestone-completion
link is just a precedence edge whose successor is a milestone, so we widened the
existing `Dependency` endpoint rule rather than adding a new field on `Milestone`
or a distinct "completes" relation. `filter_dependencies` now validates endpoints
against `task_ids | milestone_ids` (the schedulable ids), and the Dependency Agent
is handed the milestone list with an instruction to link each one from the task
that completes it. *Direction convention:* a milestone is a zero-duration
checkpoint, so it is normally an edge's **successor** (task → milestone), never a
predecessor of real work — captured in the prompt, not enforced structurally
(the scheduler handles either direction fine, so a hard rule would be
over-constraint). *The engine was already correct*, so this ticket is agent +
validation + fixture only: the golden gains four `dep-*-ms*` edges, each with a
verbatim PRD quote, and its milestones now project real dates.

**Consequences.** Milestone projection is exercised on real data, and the Gantt
payload's `projected_date` / `slack_working_days` fields (already present) light
up. `filter_dependencies`' parameter is renamed `task_ids → endpoint_ids` and its
"unknown task" rejection message generalized to "unknown id". Milestone-as-node
means a future edit could accidentally point work *at* a milestone as a
predecessor; we accept that (scheduler-valid) rather than add a guard now.

---

## ADR-0012 — Plan-of-record store: event-sourced immutable snapshots behind a repository port

**Date:** 2026-07-24 · **Ticket:** RC1-189 (P1.8) · **Status:** Accepted

**Context.** P1.8 is the "human approves" leg: nothing becomes a plan of record
until a person reviews and commits it, and committed plans must be immutable and
retrievable by version — the artifact Phase 2 baselines and Phase 3 Jira sync
build on. The ticket named "SQLite snapshots"; we revisited the storage choice
for both fit and portfolio signal.

**Explanation.** *The pattern over the engine.* The project's identity is "the
plan is an audit trail, not a black box," so the store is modeled as an
**event-sourced, append-only, content-addressed** log: each commit serializes the
plan to canonical JSON, hashes it (sha256), links to its parent, and is never
mutated. That framing — event sourcing, immutability, content-addressing,
lineage — is the resume signal, independent of the backend. *Ports and adapters.*
`planner_core` owns the domain (`Snapshot`), the storage **port**
(`PlanRepository`, a Protocol), the hash, and the commit service; the concrete
**`SQLiteEventStore`** lives in `app` (which owns the DB connection). A single
parametrized contract test runs against both the in-memory reference repo and the
SQLite adapter — the port's payoff — and a Postgres adapter can drop in for the
Phase 3 deploy behind the same interface. This keeps the "clone and run, no
credentials" property (SQLite is local-first and genuinely production-grade for a
single-user tool) while giving a real production story. *Immutability is
enforced, not conventional:* the SQLite schema installs triggers that `RAISE`
on UPDATE/DELETE, so tampering fails at the storage layer. *The commit gate* is
the human-approval leg: a plan with validation errors (unknown owner/epic,
dangling/cyclic dependency) cannot be committed, and an explicit approver is
required. *Human-vs-agent audit trail:* the agent proposal is recorded as its own
snapshot, and `diff_plans` computes the structured delta (overridden estimates,
reassigned owners, rejected/added dependencies) between it and the commit — so
where human judgment diverged from the agents is queryable.

*Scope.* Implemented as a **structured review-and-commit step** (CLI:
`propose` / `commit` / `history` / `show` / `diff`; API renders a committed
snapshot in the Gantt), not a per-proposal click-to-accept/reject editing UI —
that interactive surface overlaps RC1-197 and is deferred. Both acceptance
criteria are met: an edited plan re-schedules and its diff from the proposal is
queryable; the committed snapshot is immutable and retrievable by version.

**Consequences.** The audit trail is durable and inspectable end-to-end, closing
the phase-1 loop (PRD → reviewed, committed plan → Gantt). The chosen decision
(from a user design discussion): SQLite now, Postgres-ready via the port for
RC1-195. The interactive review UI and surfacing the decision record visually
remain tracked in RC1-197.

---

## ADR-0011 — Gantt UI: frappe-gantt over vis-timeline; the tested contract is the backend payload

**Date:** 2026-07-24 · **Ticket:** RC1-188 (P1.7) · **Status:** Accepted

**Context.** The interactive Gantt is the demo centerpiece. The ticket asked to
evaluate `frappe-gantt` vs `vis-timeline` and pick one (don't build from scratch),
and AC2 is a two-minute interview demo: the critical path and **at least one
buried-constraint dependency** must be visibly demonstrable.

**Explanation.** The two libraries split the required features almost exactly:

| Feature | frappe-gantt | vis-timeline |
| --- | --- | --- |
| Dependency arrows | **native** | none (custom SVG overlay) |
| Critical-path styling | per-bar `custom_class` | per-item `className` |
| Epic grouping | none (colour/label) | **native groups** |
| Freeze shading | overlay | **native background items** |
| Deadline line | overlay | **native custom time** |
| Milestone markers | 0-width bar | **native points** |

Chose **frappe-gantt**, because AC2 hinges on a dependency being *visible*, and
dependency arrows are the one thing that is genuinely hard to fake — vis-timeline
has no native arrows, and drawing them across group rows is the riskiest possible
thing to write for a UI I can't run here. Critical-path styling (the other AC2
half) is trivial via `custom_class`. The features frappe lacks natively are
handled without much risk: epics via colour, and freeze/deadline as SVG overlays
**calibrated from two real rendered bar positions** (solve `x = a·days + b`), so
they stay aligned regardless of frappe's internal scale — and the same facts are
always printed textually in the header, so a positioning miss degrades gracefully.
Pinned `frappe-gantt@0.6.1` for a stable, well-documented API and clean Vite ESM
interop.

*The tested contract is the backend.* The frontend isn't in the Python CI matrix
and can't be exercised headlessly, so the effort went into `app/gantt.py`
(`build_gantt_payload`) and the `/api/plan` endpoint — shape, critical-path flags,
and provenance surfaced on **both** tasks and dependency edges (the audit trail
the UI renders). The endpoint serves the scheduled flagship golden by default, so
the whole PRD→agents→engine→Gantt path renders with no credentials.

**Consequences.** The data the UI draws is locked down by tests; the rendering
itself is verified in the browser (an inherently manual, interview-style check).
Freeze shading is wired but inert until blackout windows land (RC1-196), and
milestones show projected dates once linked into the dependency graph (RC1-198).
If frappe 0.6.1's ESM import needs a nudge under Vite, that's the one spot to
adjust; the payload contract is independent of the library choice.

---

## ADR-0010 — Scheduling engine: CPM in working-day offsets, calendar mapping is separate

**Date:** 2026-07-24 · **Ticket:** RC1-187 (P1.6) · **Status:** Accepted

**Context.** The CPM engine is the project's deterministic centerpiece and the
interview answer to "where's the real engineering?" It must produce correct
early/late times, total and free float, and the critical path over the validated
dependency DAG — provably, against hand-computed textbook examples — while also
projecting onto a real working-day calendar with freeze windows and checking
hard-date deadlines.

**Explanation.** *Two clean layers.* `compute_cpm` does pure CPM in **working-day
offsets** from the project start — no dates, no calendar. That is exactly what
makes it testable against textbook networks (the A–B–C–D–E–F example asserts
every ES/EF/LS/LF/float and the A-B-D-F critical path). A separate
`WorkingCalendar` maps offsets → dates, and it is the *only* thing that knows
about weekends and blackout/freeze windows. *Freeze windows are just non-working
days.* A freeze is a stretch of days on which nobody works, identical to a
weekend, so it changes no float and no offset — the schedule simply flows around
it when offsets are mapped to dates. This falls straight out of the layering and
needs no special CPM logic. Blackouts are passed explicitly for now; extracting
them from constraints waits on RC1-196's first-class blackout-window type (today
the Q4 freeze is text inside a gate constraint). *All four PDM relationship types
+ lags* (FS/SS/FF/SF) are supported in both passes — "FS first, extensible," done
properly. *Free float* is computed from forward edge-slack (`ES(succ) −
required_ES`), which is correct for every relationship type because shifting a
task later moves its ES and EF together. *Durations are the `likely` estimate*
(per the ticket), not the PERT expected value. *Hard-date checks* use a
constraint's `applies_to` to find the targeted task and report signed
working-day slack (negative = the plan misses the date). *Milestones* are
scheduled as zero-duration nodes and reported as scheduled **only when a
dependency edge reaches them**; on today's plans milestones are unlinked (the
Dependency Agent targets tasks only), so they carry their target date but no
projection — the linked path is unit-tested to prove the projected-date/slack
output. The `plan schedule` CLI is fully deterministic (no LLM), so it runs on
the golden directly: 51 working days, an 11-task critical path along the
migration spine, clearing the license wall with 111 days of slack.

**Consequences.** The engine is correct-by-construction and exhaustively tested,
independent of any calendar or model. Two known gaps are deliberate: freeze
auto-extraction from constraints is blocked on RC1-196, and per-milestone
projections need milestones wired into the dependency graph (a small extension to
the Dependency Agent). Fractional durations render at whole-working-day calendar
granularity (offsets floor/ceil to days); the underlying float math stays exact.

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
