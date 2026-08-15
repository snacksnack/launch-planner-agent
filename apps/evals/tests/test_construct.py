"""Construct validity — the check that needs no human labels."""

from __future__ import annotations

from evals import construct
from evals.rubric import Score


def _labels(spec):
    """`{"a": ("fallback", 2), ...}` -> (labels, variants)."""
    labels, variants = {}, {}
    for seed_id, (variant, score) in spec.items():
        labels[seed_id] = {"no-unsupported-claims": Score(score), "tone": Score(score)}
        variants[seed_id] = variant
    return labels, variants


def _first_judged(spec):
    labels, variants = _labels(spec)
    return next(
        r for r in construct.separation(labels, variants) if r.dimension == "no-unsupported-claims"
    )


def test_a_scorer_that_ranks_clean_above_planted_detects_it():
    result = _first_judged({"a": ("fallback", 2), "b": ("degraded", 0)})
    assert result.rate == 1.0
    assert result.detects


def test_an_inverted_scorer_scores_zero():
    """Two human passes did exactly this — scored the template-generated output
    below the deliberately degraded one."""
    result = _first_judged({"a": ("fallback", 0), "b": ("degraded", 2)})
    assert result.rate == 0.0
    assert not result.detects


def test_ties_count_half_so_a_flat_scorer_lands_at_chance():
    """A scorer giving everything the same score has no signal, and must not
    look like one — the same trap `agreement.py` guards for kappa."""
    result = _first_judged({"a": ("fallback", 2), "b": ("degraded", 2)})
    assert result.rate == 0.5
    assert not result.detects


def test_untargeted_dimensions_are_out_of_scope_not_failures():
    """The degraded prompt attacks groundedness and tone. Scoring completeness
    against it reported 33% — which reads as a judge failure and is really a
    statement about the prompt."""
    labels, variants = _labels({"a": ("fallback", 2), "b": ("degraded", 0)})
    by_dimension = {r.dimension: r for r in construct.separation(labels, variants)}

    assert by_dimension["no-unsupported-claims"].targeted
    assert by_dimension["tone"].targeted
    assert not by_dimension["completeness"].targeted
    assert not by_dimension["actionability"].targeted
    # Even a perfect separation earns nothing on an untargeted dimension.
    assert not by_dimension["completeness"].detects


def test_the_rank_statistic_ignores_a_uniform_offset():
    """A judge that is harsh across the board still separates correctly; a mean
    difference would confuse that offset with signal."""
    strict = _first_judged({"a": ("fallback", 1), "b": ("degraded", 0)})
    lenient = _first_judged({"a": ("fallback", 2), "b": ("degraded", 1)})
    assert strict.rate == lenient.rate == 1.0


def test_missing_variants_do_not_crash():
    result = _first_judged({"a": ("fallback", 2)})
    assert result.pairs == 0
    assert result.rate == 0.0
    assert not result.detects


def test_passing_the_check_never_confers_gating_rights():
    """The floor is agreement with a human. This check is weaker and says so —
    a judge can separate a planted extreme and still disagree on the middle."""
    assert construct.SEPARATION_FLOOR < 1.0
    result = _first_judged({"a": ("fallback", 2), "b": ("degraded", 0)})
    assert result.detects
    assert not hasattr(result, "gates"), "construct results must not expose a gating verdict"
