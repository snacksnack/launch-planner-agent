"""Tests for the Monte Carlo launch-date confidence band (RC1-201).

The acceptance criteria: over the flagship golden, the run yields a launch-date
distribution with sensible P50/P80/P90 and a per-task criticality index; and it is
deterministic for a fixed seed. Small hand-built plans pin the sampler and the
percentile ordering; the golden anchors the point estimate and criticality.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from random import Random

import pytest
from planner_core import (
    Confidence,
    Dependency,
    Plan,
    Provenance,
    Task,
    TeamMember,
    ThreePointEstimate,
    monte_carlo,
    pert_ppf,
    sample_pert,
    schedule_plan,
)
from planner_core.monte_carlo import _betainc

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "jira-cloud-migration"
MONDAY = date(2026, 8, 3)


def _prov() -> Provenance:
    return Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=datetime(2026, 7, 26, tzinfo=UTC),
    )


def _task(tid: str, o: float, m: float, p: float) -> Task:
    return Task(
        id=tid, name=tid, owner_id="tm-1",
        estimate=ThreePointEstimate(optimistic=o, likely=m, pessimistic=p),
        provenance=_prov(),
    )


def _dep(pred: str, succ: str) -> Dependency:
    return Dependency(
        id=f"dep-{pred}-{succ}", predecessor_id=pred, successor_id=succ, provenance=_prov()
    )


def _plan(tasks, deps) -> Plan:
    return Plan(
        id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")], tasks=tasks, dependencies=deps
    )


def _golden() -> Plan:
    return Plan.model_validate_json((FIXTURE / "golden" / "expected-plan.json").read_text())


# --- the sampler -----------------------------------------------------------


def test_sample_pert_stays_within_bounds():
    rng = Random(0)
    for _ in range(500):
        x = sample_pert(3, 5, 12, rng)
        assert 3 <= x <= 12


def test_sample_pert_degenerate_range_is_constant():
    rng = Random(0)
    assert sample_pert(5, 5, 5, rng) == 5
    # optimistic == pessimistic but likely nominally between: still a point.
    assert sample_pert(4, 4, 4, rng) == 4


def test_sample_pert_mean_approximates_the_pert_expectation():
    # Beta-PERT mean is (o + 4m + p) / 6; average of many draws should be close.
    rng = Random(1)
    o, m, p = 2, 6, 16
    n = 20_000
    avg = sum(sample_pert(o, m, p, rng) for _ in range(n)) / n
    expected = (o + 4 * m + p) / 6  # = 7.0
    assert abs(avg - expected) < 0.2


# --- determinism -----------------------------------------------------------


def test_deterministic_for_a_fixed_seed():
    plan = _golden()
    a = monte_carlo(plan, start_date=MONDAY, iterations=300, seed=42)
    b = monte_carlo(plan, start_date=MONDAY, iterations=300, seed=42)
    assert a.model_dump() == b.model_dump()


def test_a_different_seed_gives_a_different_draw():
    plan = _golden()
    a = monte_carlo(plan, start_date=MONDAY, iterations=300, seed=1)
    b = monte_carlo(plan, start_date=MONDAY, iterations=300, seed=2)
    # The full distributions should not be identical.
    assert a.model_dump() != b.model_dump()


# --- percentiles -----------------------------------------------------------


def test_percentiles_are_non_decreasing():
    plan = _golden()
    r = monte_carlo(plan, start_date=MONDAY, iterations=500, seed=7)
    assert r.p10 <= r.p50 <= r.p80 <= r.p90


def test_point_estimate_matches_the_deterministic_cpm_finish():
    # The reference finish uses the likely durations — it must equal schedule_plan.
    plan = _golden()
    r = monte_carlo(plan, start_date=MONDAY, iterations=100, seed=0)
    sched = schedule_plan(plan, start_date=MONDAY)
    assert r.deterministic_finish == sched.project_finish_date


def test_confidence_band_brackets_the_point_estimate_on_a_skewed_plan():
    # A -> B, both right-skewed (long pessimistic tail). The P90 finish should land
    # later than the likely-estimate point finish; P10 no later than it.
    plan = _plan(
        [_task("A", 2, 4, 20), _task("B", 2, 4, 20)],
        [_dep("A", "B")],
    )
    r = monte_carlo(plan, start_date=MONDAY, iterations=1000, seed=3)
    assert r.p10 <= r.deterministic_finish
    assert r.p90 > r.deterministic_finish


# --- criticality index -----------------------------------------------------


def test_criticality_index_is_a_probability_and_sorted():
    plan = _golden()
    r = monte_carlo(plan, start_date=MONDAY, iterations=400, seed=11)
    assert r.criticality  # non-empty
    assert all(0.0 <= tc.criticality <= 1.0 for tc in r.criticality)
    # sorted most-critical first
    vals = [tc.criticality for tc in r.criticality]
    assert vals == sorted(vals, reverse=True)


def test_always_critical_task_has_index_one():
    # A(10) -> C, B(1) -> C. A always dominates, so A and C are always critical;
    # B (huge float) never is.
    plan = _plan(
        [_task("A", 10, 10, 10), _task("B", 1, 1, 1), _task("C", 3, 3, 3)],
        [_dep("A", "C"), _dep("B", "C")],
    )
    r = monte_carlo(plan, start_date=MONDAY, iterations=200, seed=0)
    idx = {tc.task_id: tc.criticality for tc in r.criticality}
    assert idx["A"] == 1.0
    assert idx["C"] == 1.0
    assert idx["B"] == 0.0


def test_distribution_histogram_counts_sum_to_iterations():
    # Every run has a finish >= 1 day here, so all land in the histogram.
    plan = _plan([_task("A", 2, 4, 8)], [])
    r = monte_carlo(plan, start_date=MONDAY, iterations=250, seed=5)
    assert sum(b["count"] for b in r.distribution) == 250
    # buckets are date-sorted
    dates = [b["date"] for b in r.distribution]
    assert dates == sorted(dates)


# --- the quantile function (RC1-209) ---------------------------------------


def test_betainc_matches_known_values():
    # I_x(1, 1) is the uniform CDF — the one closed form worth pinning.
    for x in (0.0, 0.25, 0.5, 0.9, 1.0):
        assert abs(_betainc(1, 1, x) - x) < 1e-12
    # and the reflection identity I_x(a, b) == 1 - I_(1-x)(b, a)
    assert abs(_betainc(2, 5, 0.3) - (1 - _betainc(5, 2, 0.7))) < 1e-12


def test_pert_ppf_is_bounded_and_monotone():
    o, m, p = 2, 6, 16
    assert pert_ppf(0.0, o, m, p) == o
    assert pert_ppf(1.0, o, m, p) == p
    draws = [pert_ppf(u / 20, o, m, p) for u in range(1, 20)]
    assert draws == sorted(draws)
    assert all(o <= d <= p for d in draws)


def test_pert_ppf_degenerate_range_is_constant():
    assert pert_ppf(0.3, 5, 5, 5) == 5


def test_pert_ppf_reproduces_the_pert_mean():
    # Averaging the quantile function over a uniform grid is the distribution mean,
    # which for Beta-PERT is (o + 4m + p) / 6 — the same law sample_pert draws from.
    o, m, p = 2, 6, 16
    n = 2000
    avg = sum(pert_ppf((i + 0.5) / n, o, m, p) for i in range(n)) / n
    assert abs(avg - (o + 4 * m + p) / 6) < 0.01


# --- correlated durations (RC1-209) ----------------------------------------


def test_zero_correlation_is_the_independent_default():
    # The contract that keeps RC1-201's numbers valid: at strength 0 nothing changes,
    # because the sampler takes the original draw path.
    plan = _golden()
    a = monte_carlo(plan, start_date=MONDAY, iterations=400, seed=42)
    b = monte_carlo(plan, start_date=MONDAY, iterations=400, seed=42, correlation=0.0)
    assert a.model_dump() == b.model_dump()


def test_correlation_widens_the_tail_but_holds_the_median():
    # The whole point: common-cause risk fattens the tail without moving the middle.
    plan = _golden()
    independent = monte_carlo(plan, start_date=MONDAY, iterations=2000, seed=42)
    correlated = monte_carlo(
        plan, start_date=MONDAY, iterations=2000, seed=42, correlation=0.5
    )
    assert correlated.p80 > independent.p80
    assert correlated.p90 > independent.p90
    assert abs((correlated.p50 - independent.p50).days) <= 3


def test_higher_correlation_pushes_the_tail_further_out():
    plan = _golden()
    p90s = [
        monte_carlo(
            plan, start_date=MONDAY, iterations=1500, seed=5, correlation=rho
        ).p90
        for rho in (0.0, 0.3, 0.6, 1.0)
    ]
    assert p90s == sorted(p90s)
    assert p90s[-1] > p90s[0]


def test_correlated_runs_are_deterministic_for_a_fixed_seed():
    plan = _golden()
    a = monte_carlo(plan, start_date=MONDAY, iterations=300, seed=9, correlation=0.4)
    b = monte_carlo(plan, start_date=MONDAY, iterations=300, seed=9, correlation=0.4)
    assert a.model_dump() == b.model_dump()


def test_correlation_is_reported_on_the_result():
    plan = _plan([_task("A", 2, 4, 8)], [])
    assert monte_carlo(plan, start_date=MONDAY, iterations=50, correlation=0.25).correlation == 0.25


@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
def test_correlation_outside_zero_to_one_is_rejected(bad):
    plan = _plan([_task("A", 2, 4, 8)], [])
    with pytest.raises(ValueError, match="correlation"):
        monte_carlo(plan, start_date=MONDAY, iterations=10, correlation=bad)


def test_full_correlation_removes_the_merge_bias():
    """Two identical parallel tasks feeding one finish — the §6 merge-bias setup.

    Independently, the finish is the *max* of two draws, so it lands later than either
    task alone. At correlation 1 both sit at the same quantile, so there is no max to
    take and the merge bias vanishes.
    """
    both = _plan([_task("A", 2, 6, 20), _task("B", 2, 6, 20)], [])
    one = _plan([_task("A", 2, 6, 20)], [])
    kw = {"start_date": MONDAY, "iterations": 3000, "seed": 3}

    independent_gap = (
        monte_carlo(both, **kw).p50 - monte_carlo(one, **kw).p50
    ).days
    locked_gap = (
        monte_carlo(both, correlation=1.0, **kw).p50
        - monte_carlo(one, correlation=1.0, **kw).p50
    ).days

    assert independent_gap > 0  # the merge bias is real when durations are independent
    assert abs(locked_gap) <= 1  # and gone when they move together
