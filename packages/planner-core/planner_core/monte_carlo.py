"""Monte Carlo schedule risk — a confidence band on the launch date (RC1-201).

The deterministic CPM schedule uses each task's *likely* estimate and reports one
finish date. But estimates are ranges, and the critical path can shift when they
move. This samples each task's duration from its three-point estimate, re-runs CPM
many times, and reads the distribution of the projected launch: "80% chance of
launching on or before <date>", plus each task's **criticality index** — how often
it lands on the critical path across the runs.

Sampling uses the **Beta-PERT** distribution (the classic three-point model:
optimistic / most-likely / pessimistic). It's deterministic given a seed — the RNG
is seeded and passed in like `start_date`, so a run is reproducible and testable;
there is no randomness anywhere near the frontend. Pure `planner_core`: it reuses
`compute_cpm` and the working-day calendar, nothing else.

Durations may be sampled **independently** (the default) or with a shared risk
factor — see `correlation` on `monte_carlo` and §"Correlated durations" in
`docs/forecasting.md`. Correlated sampling uses a one-factor Gaussian copula, which
needs the Beta quantile function; `_betainc`/`_beta_ppf` below implement it directly
so the core keeps its two dependencies (RC1-209).
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date
from random import Random
from statistics import NormalDist

from pydantic import BaseModel, ConfigDict

from planner_core.models import Plan
from planner_core.scheduling import (
    _DEFAULT_WEEKEND,
    _EPS,
    EdgeSpec,
    WorkingCalendar,
    blackout_windows,
    compute_cpm,
)


def sample_pert(optimistic: float, likely: float, pessimistic: float, rng: Random) -> float:
    """Draw one duration from the Beta-PERT distribution of a three-point estimate.

    Beta-PERT puts most mass near `likely` while respecting the optimistic and
    pessimistic bounds — the standard model for PM three-point estimates. Degenerate
    when the range is a point.
    """
    if pessimistic <= optimistic:
        return optimistic
    span = pessimistic - optimistic
    alpha = 1 + 4 * (likely - optimistic) / span
    beta = 1 + 4 * (pessimistic - likely) / span
    return optimistic + span * rng.betavariate(alpha, beta)


_STANDARD_NORMAL = NormalDist()


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion for the incomplete beta (Lentz's method)."""
    tiny, eps, max_iter = 1e-300, 3e-16, 300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        # even step, then odd step — each refines one level of the fraction
        for numerator in (
            m * (b - m) * x / ((qam + m2) * (a + m2)),
            -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2)),
        ):
            d = 1.0 + numerator * d
            if abs(d) < tiny:
                d = tiny
            c = 1.0 + numerator / c
            if abs(c) < tiny:
                c = tiny
            d = 1.0 / d
            h *= d * c
        if abs(d * c - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta I_x(a, b) — the Beta CDF.

    Stdlib has no beta function, and `planner-core` deliberately carries only
    pydantic and networkx, so this is computed directly from `math.lgamma`.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    # The fraction converges fast only on one side of the mode; flip when it doesn't.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(u: float, a: float, b: float) -> float:
    """Invert the Beta CDF by bisection — monotone, so this always converges."""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _betainc(a, b, mid) < u:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2.0


def pert_ppf(u: float, optimistic: float, likely: float, pessimistic: float) -> float:
    """The Beta-PERT quantile function: the duration at cumulative probability `u`.

    The inverse of `sample_pert`'s distribution, taking the randomness as an argument
    instead of drawing it. That's what lets a copula correlate draws while leaving
    each task's marginal distribution exactly Beta-PERT.
    """
    if pessimistic <= optimistic:
        return optimistic
    span = pessimistic - optimistic
    alpha = 1 + 4 * (likely - optimistic) / span
    beta = 1 + 4 * (pessimistic - likely) / span
    return optimistic + span * _beta_ppf(u, alpha, beta)


class TaskCriticality(BaseModel):
    """How often a task landed on the critical path across the runs."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    name: str
    criticality: float  # 0..1


class MonteCarloResult(BaseModel):
    """The launch-date distribution and criticality index from a Monte Carlo run."""

    model_config = ConfigDict(extra="forbid")

    iterations: int
    seed: int
    correlation: float  # 0 = independent durations; 1 = one shared risk factor
    start_date: date
    deterministic_finish: date | None  # the point estimate (likely durations)
    p10: date | None
    p50: date | None
    p80: date | None
    p90: date | None
    mean_working_days: float
    distribution: list[dict]  # [{"date": iso, "count": n}] — a finish-date histogram
    criticality: list[TaskCriticality]  # sorted most-critical first


def _finish_date(duration: float, calendar: WorkingCalendar) -> date | None:
    """The calendar finish date for a project of `duration` working days."""
    if duration < 1:
        return None
    return calendar.nth_working_day(math.ceil(duration - _EPS) - 1)


def monte_carlo(
    plan: Plan,
    *,
    start_date: date,
    iterations: int = 1000,
    seed: int = 0,
    correlation: float = 0.0,
    weekend: frozenset[int] = _DEFAULT_WEEKEND,
    blackouts: tuple[tuple[date, date], ...] = (),
) -> MonteCarloResult:
    """Sample durations, re-run CPM `iterations` times, and summarize the launch date.

    `correlation` (0..1) is how strongly task durations move together. At 0 each task
    is drawn independently — the RC1-201 behaviour, reproduced exactly. Above 0 a
    one-factor Gaussian copula gives every task a shared latent risk factor, so a bad
    run tends to be bad for everyone: the tail widens while P50 barely moves. At 1
    every task sits at the same quantile of its own distribution.
    """
    if not 0.0 <= correlation <= 1.0:
        raise ValueError(f"correlation must be between 0 and 1, got {correlation}")
    rng = Random(seed)
    calendar = WorkingCalendar(
        start_date=start_date,
        weekend=weekend,
        blackouts=tuple(blackouts) + blackout_windows(plan),
    )
    edges: list[EdgeSpec] = [
        (d.predecessor_id, d.successor_id, d.type, d.lag) for d in plan.dependencies
    ]
    triples = {
        t.id: (t.estimate.optimistic, t.estimate.likely, t.estimate.pessimistic)
        for t in plan.tasks
    }
    milestone_ids = [m.id for m in plan.milestones]

    finish_durations: list[float] = []
    critical_hits = dict.fromkeys(triples, 0)

    # At zero correlation, keep the original draw path exactly — same RNG calls in the
    # same order — so an independent run reproduces RC1-201's numbers bit for bit.
    common_weight = math.sqrt(correlation)
    idiosyncratic_weight = math.sqrt(1.0 - correlation)

    for _ in range(iterations):
        if correlation == 0.0:
            durations = {
                tid: sample_pert(o, m, p, rng) for tid, (o, m, p) in triples.items()
            }
        else:
            shared = rng.gauss(0.0, 1.0)  # one draw per run: the common-cause factor
            durations = {}
            for tid, (o, m, p) in triples.items():
                z = common_weight * shared + idiosyncratic_weight * rng.gauss(0.0, 1.0)
                durations[tid] = pert_ppf(_STANDARD_NORMAL.cdf(z), o, m, p)
        for mid in milestone_ids:
            durations[mid] = 0.0
        result = compute_cpm(durations, edges)
        finish_durations.append(result.project_duration)
        for tid in critical_hits:
            if result.nodes[tid].is_critical:
                critical_hits[tid] += 1

    finish_durations.sort()

    def percentile(pct: int) -> date | None:
        if not finish_durations:
            return None
        # nearest-rank: the smallest finish that at least `pct`% of runs meet.
        idx = max(0, math.ceil(pct / 100 * len(finish_durations)) - 1)
        return _finish_date(finish_durations[idx], calendar)

    histogram = Counter()
    for d in finish_durations:
        fd = _finish_date(d, calendar)
        if fd is not None:
            histogram[fd.isoformat()] += 1
    distribution = [{"date": k, "count": v} for k, v in sorted(histogram.items())]

    # The point estimate: CPM on the likely durations, for reference.
    likely_durations = {t.id: t.estimate.likely for t in plan.tasks}
    for mid in milestone_ids:
        likely_durations[mid] = 0.0
    deterministic = _finish_date(compute_cpm(likely_durations, edges).project_duration, calendar)

    name_of = {t.id: t.name for t in plan.tasks}
    criticality = sorted(
        (
            TaskCriticality(task_id=tid, name=name_of[tid], criticality=hits / iterations)
            for tid, hits in critical_hits.items()
        ),
        key=lambda tc: (-tc.criticality, tc.task_id),
    )

    return MonteCarloResult(
        iterations=iterations,
        seed=seed,
        correlation=correlation,
        start_date=start_date,
        deterministic_finish=deterministic,
        p10=percentile(10),
        p50=percentile(50),
        p80=percentile(80),
        p90=percentile(90),
        mean_working_days=(
            sum(finish_durations) / len(finish_durations) if finish_durations else 0.0
        ),
        distribution=distribution,
        criticality=criticality,
    )
