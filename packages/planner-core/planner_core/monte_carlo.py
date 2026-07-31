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
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date
from random import Random

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
    weekend: frozenset[int] = _DEFAULT_WEEKEND,
    blackouts: tuple[tuple[date, date], ...] = (),
) -> MonteCarloResult:
    """Sample durations, re-run CPM `iterations` times, and summarize the launch date."""
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

    for _ in range(iterations):
        durations = {
            tid: sample_pert(o, m, p, rng) for tid, (o, m, p) in triples.items()
        }
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
