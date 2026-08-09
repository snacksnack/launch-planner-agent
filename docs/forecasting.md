# Forecasting — the Monte Carlo launch-date confidence band

*How `planner_core.monte_carlo` turns three-point estimates into a probabilistic
launch date. Companion to the deterministic scheduler in
[architecture.md](architecture.md); the design-decision record is
[ADR-0022](decisions.md).*

---

## 1. The problem it solves

The deterministic schedule (`schedule_plan`) runs the Critical Path Method once,
using each task's **most-likely** estimate, and reports a single launch date. That
date is easy to read and easy to over-trust, for two reasons:

1. **Estimates are ranges, not points.** Every task carries an optimistic, a
   most-likely, and a pessimistic estimate. Collapsing that to one number throws
   away everything we know about the *spread*.
2. **The critical path is not fixed.** A task with slack today can consume its
   float and *join* the critical path once its duration drifts. A single pass can't
   see that; it reports the one path that happened to be critical for one set of
   durations.

The consequence isn't just "the date is uncertain" — it's that **the single-point
date is systematically optimistic** (§6 explains why). The forecast exists to
quantify both the spread and the bias, and to answer the question executives
actually ask: *not* "when will it land?" but "**how confident are we** in a date?"

---

## 2. The input: three-point estimates

Each `Task` already carries a `ThreePointEstimate`:

```python
ThreePointEstimate(optimistic=3, likely=5, pessimistic=12)  # working days
```

- **optimistic (o)** — everything goes right.
- **likely (m)** — the mode; the single value the deterministic schedule uses.
- **pessimistic (p)** — plausible worst case.

The deterministic scheduler uses only `m`. The forecast uses all three: they
define the *shape* of each task's duration distribution.

---

## 3. The sampler: Beta-PERT

For each task, on each iteration, we draw one duration from a **Beta-PERT**
distribution — the standard three-point model in project-risk analysis. The whole
sampler (`sample_pert`) is five lines:

```python
def sample_pert(optimistic, likely, pessimistic, rng):
    if pessimistic <= optimistic:          # degenerate range → a constant
        return optimistic
    span  = pessimistic - optimistic
    alpha = 1 + 4 * (likely - optimistic) / span
    beta  = 1 + 4 * (pessimistic - likely) / span
    return optimistic + span * rng.betavariate(alpha, beta)
```

### What it is

A **Beta distribution rescaled onto the interval `[o, p]`.** `rng.betavariate(α, β)`
returns a value in `[0, 1]`; we stretch it across the estimate's range with
`o + span · x`.

### Why Beta

The Beta family is the natural fit for a bounded, skewable quantity:

- **Bounded** — a sampled duration can never fall below `o` or above `p`. (A normal
  distribution would put mass on impossible negative or absurdly large durations.)
- **Flexible shape** — by choosing α and β we can make it symmetric or skewed, peaked
  or flat.

### Where the α/β formulas come from

They are the **classic PERT parameterization**. They are chosen so that the
distribution's mode sits at `m` and its mean is the familiar PERT expectation:

```
mean = (o + 4·m + p) / 6
```

That is the well-known PERT weighting — it leans **4×** on the most-likely value and
1× on each extreme. The `4`s in the α/β formulas are that same weight.

### Why it's usually right-skewed (and why that matters)

Compare the two half-ranges:

- optimistic side: `m − o`
- pessimistic side: `p − m`

When the pessimistic tail is longer (`p − m > m − o`) — which is the realistic case,
because tasks slip more often and more severely than they beat estimate — α < β and
the distribution **leans right**. Its *mean is later than its mode*. So even before
any scheduling, the average sampled duration for a task is a bit longer than its
"likely" value. That skew, aggregated across a plan, is a big part of the story.

### Beta-PERT vs. the alternatives

- **vs. triangular** — triangular (straight-line sides from o to m to p) is cruder: a
  sharp peak and hard corners. Beta-PERT is smooth and is what commercial risk tools
  (@RISK, Primavera Risk) use. We chose it deliberately.
- **degenerate ranges** — when `o == p` (milestones, fixed-duration tasks) the sampler
  short-circuits to a constant, so zero-variance work contributes no noise and costs
  nothing.

---

## 4. The Monte Carlo loop

The engine builds the expensive, invariant structure **once**, then iterates cheaply:

```python
# built once, before the loop:
calendar = WorkingCalendar(start_date, weekend, blackouts)   # weekend/freeze aware
edges    = [(d.predecessor_id, d.successor_id, d.type, d.lag) for d in plan.dependencies]
triples  = {t.id: (o, m, p) for each task}

for _ in range(iterations):                    # default 1000
    durations = {tid: sample_pert(o, m, p, rng) for tid, (o, m, p) in triples.items()}
    for milestone_id in milestone_ids:
        durations[milestone_id] = 0.0
    result = compute_cpm(durations, edges)     # the SAME engine the deterministic path uses
    finish_durations.append(result.project_duration)
    for tid in critical_hits:
        if result.nodes[tid].is_critical:
            critical_hits[tid] += 1
```

Two things worth stressing in an interview:

- **It reuses `compute_cpm`.** We don't have a second, forkable copy of the
  scheduling math. Each iteration only changes the *durations* dict and re-runs the
  exact CPM engine the deterministic schedule uses. One source of truth for the math.
- **No plan copies.** We never deep-copy the `Plan`. Only a small `{id: float}` dict
  is rebuilt per iteration, which is why 1,000 runs over the flagship land in
  **~0.1 s** (see §7).

Each iteration records exactly two things: the **project finish** (as a working-day
duration) and **which tasks were critical**. Everything the forecast reports is
aggregated from those two.

---

## 5. What comes out

### The confidence band (percentiles)

Sort the 1,000 finish durations. A percentile is read by **nearest-rank**: P80 is the
value at rank `ceil(0.80 × N)`. Each percentile duration is then mapped to a calendar
date through the **same working-day calendar** the deterministic schedule uses
(skipping weekends and blackout/freeze windows) — so the point estimate the panel
shows is *identical* to the deterministic CPM finish, and the percentiles are
directly comparable to it.

Read it as: **"P80 = Oct 23" means 800 of the 1,000 runs finished on or before
Oct 23** — an 80% confidence date.

### The distribution histogram

The finish dates bucketed and counted — the shape you see plotted in the Forecast
panel, with the P50/P80/P90 and the deterministic "Likely" estimate marked on it.

### The criticality index

For each task, the **fraction of runs in which it was on the critical path**. This is
richer than the deterministic pass's binary critical/not-critical: a task with slack
in the single deterministic schedule might be critical in, say, 78% of runs. That
number flags it as a **latent schedule driver** — something worth watching that a
one-shot CPM pass would quietly label "safe." The panel ranks tasks by this index;
they are the true, risk-weighted drivers of the launch date.

---

## 6. Why the single-point date is *biased*, not just uncertain

This is the theoretical heart of the feature — the point most worth being able to
explain.

The project finish is a **maximum over converging paths**. When several chains of
work merge into one downstream task (in the flagship: the pilot migration *and* the
tooling setup *and* the runbook all feed the bulk migration), the merge point can't
start until the **slowest** incoming path is done.

Maximum is a **convex** function, and for convex functions Jensen's inequality gives:

```
E[ finish(durations) ]  ≥  finish( E[durations] )
```

In words: **the expected finish date is later than the finish computed from average
durations.** The deterministic schedule computes the right-hand side (one finish from
the mode/mean durations); reality behaves like the left-hand side (the average over
many random outcomes). The gap between them is the optimism bias — sometimes called
**merge bias**: the more parallel paths converge on a milestone, the more likely it is
that *at least one* of them runs long, so the merge slips more often than any single
path would suggest.

On the flagship golden this is stark: the deterministic plan lands **Oct 12**, but
only **~19% of the runs actually finish by then** — 80% confidence is **Oct 23**. That
11-day gap isn't noise around the point estimate; it's a structural bias the single
pass can't reveal, and quantifying it is the entire reason the forecast earns its
place.

---

## 7. Determinism and performance

**Deterministic by construction.** The randomness lives in a *seeded, local* RNG
instance — `random.Random(seed)` — passed in exactly like `start_date`. It's a private
generator, not the global `random` module, so nothing else in the process perturbs it.
Same seed + same plan ⇒ **byte-identical output** (there is a test asserting exactly
this). Crucially, **no randomness reaches the frontend**: the browser just issues
`GET /api/forecast?seed=…` and renders whatever Python computed. This preserves the
system-wide principle — *the LLM proposes; Python computes, deterministically and
reproducibly* — and keeps the forecast unit-testable.

**Fast.** Because the calendar and edge list are built once and each iteration is a
dict-sample plus one `compute_cpm` call over ~25 nodes, **1,000 iterations run in
~0.1 s**. Iteration count is a parameter (`--iterations`, default 1000; the API caps
it at 5000) so you can trade precision for speed.

---

## 8. Assumptions and limitations

State these before an interviewer finds them:

- **Independent sampling — by default, but no longer forced.** Each task's duration is
  drawn independently unless you ask for correlation; see §8b below. Independence makes
  the band **somewhat too narrow** in the tail, so a default run is the optimistic read
  on spread.
- **Beta-PERT's fixed weighting.** The `4×` mode weight is the standard PERT
  assumption about how concentrated the distribution is around the mode. A "modified
  PERT" exposes that weight as a tunable parameter; we use the classic value.
- **Garbage in, garbage out.** The forecast is only as good as the o/m/p estimates
  feeding it. Here they come from the hand-reviewed golden plan (or, upstream, the
  LLM work-breakdown) — which is exactly why every estimate carries provenance.

None of these undermine the result; they scope it. The forecast is an honest,
reproducible read on schedule risk *given* the estimates — not a crystal ball.

---

## 8b. Correlated durations

Real delays travel in packs. A hard integration, a team that never freed up, an
estimator who is optimistic about everything — each slips *several* tasks at once.
Sampling every task independently quietly assumes the opposite, and independent errors
partly cancel, so the tail comes out too thin.

`--correlation ρ` (0 to 1, default 0) turns that off. Each iteration draws **one shared
factor** and gives every task a blend of it and its own noise:

```
z₀ ~ N(0,1)                     # the common cause, drawn once per run
zᵢ = √ρ·z₀ + √(1−ρ)·zᵢ′         # each task: shared part + its own part
dᵢ = PERT⁻¹( Φ(zᵢ) )            # back to a duration via the quantile function
```

This is a **one-factor Gaussian copula**. The important property: correlation changes
only *which quantile* each task lands on, never the shape of its own distribution. Every
task is still exactly Beta-PERT — so the middle of the forecast stays put and only the
spread responds. On the golden at seed 42 and the default 1,000 iterations:

| ρ | P10 | P50 | P80 | P90 |
|---|---|---|---|---|
| 0.0 (independent) | Oct 9 | Oct 16 | Oct 23 | Oct 27 |
| 0.4 | Oct 2 | Oct 16 | Oct 27 | Nov 2 |
| 1.0 (lockstep) | Sep 25 | Oct 15 | Oct 29 | Nov 5 |

Two honest notes. First, the band widens in **both** directions — P10 pulls earlier as
P90 pushes later — because a shared factor makes good runs good for everyone too. The
late tail is the one that matters commercially, but the model isn't one-sided and this
doc won't pretend it is. Second, **ρ is a judgement, not a measurement.** Nobody can
read "0.4" off a project. Treat it as a stress test — "if this team's delays move
together, how much later is the safe date?" — rather than a calibrated input. That is
also why there is no slider in the UI: the panel shows one defensible default instead
of inviting a number nobody can justify.

At `ρ = 0` the sampler takes the original independent code path, so an uncorrelated run
is **bit-for-bit identical** to what it produced before this feature existed. The
determinism guarantee in §7 is unchanged: same seed, same ρ, same output.

---

## 9. Where it lives, and how to run it

| Layer | Location |
|---|---|
| **Engine** (pure, no LLM/app deps) | `packages/planner-core/planner_core/monte_carlo.py` |
| **CLI verb** | `plan forecast <plan> --start-date … [--iterations N] [--seed S] [--correlation ρ]` |
| **API** (read-only) | `GET /api/forecast?start=…&iterations=…&seed=…&correlation=…` |
| **UI panel** | the **Forecast** button in the web toolbar |
| **Tests** | `packages/planner-core/tests/test_monte_carlo.py`, plus `apps/api/tests/test_gantt.py`; frontend geometry in `apps/web/tests/lib.test.js` |

```bash
# The flagship golden, reproducible for a fixed seed:
uv run plan forecast fixtures/jira-cloud-migration/golden/expected-plan.json \
    --start-date 2026-08-03 --seed 42
```

```
Launch-date forecast — 1000 runs, seed 42 (Beta-PERT over three-point estimates, independent)
  Point estimate (likely durations): 2026-10-12

  Confidence band (chance of launching on or before):
    P10  2026-10-09   P50  2026-10-16   P80  2026-10-23   P90  2026-10-27
    → 80% confidence: on or before 2026-10-23.
  ▁▁▁▃▃▄▅▆▇▆█▇█▇▇▆▅▄▄▃▃▂▂▂▁▁▁

  Criticality index (how often each task is on the critical path):
      100%  Bulk-migrate the remaining 18 projects in waves
       99%  Pilot-migrate two low-risk projects
       99%  Validate pilot data with project owners
       ...
```

The sparkline is the finish-date distribution: a smooth right-skewed hump — exactly
what §3 (skew) and §6 (merge bias) predict.
