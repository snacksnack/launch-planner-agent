"""Agreement maths. Pure, free, and the part that has to be right — every
conclusion in `docs/judging.md` is downstream of these numbers."""

from __future__ import annotations

from evals.agreement import GATING_FLOOR, compare, confusion
from evals.rubric import DIMENSION_KEYS, Score


def _labels(*per_seed):
    """`_labels((2, 2, 2, 2), ...)` -> {seed_id: {dimension: Score}}."""
    return {
        f"s{index}": dict(zip(DIMENSION_KEYS, [Score(v) for v in scores], strict=True))
        for index, scores in enumerate(per_seed)
    }


def _one_dimension(human_scores, judge_scores):
    """Vary only groundedness; hold the rest identical so it is isolated."""
    human = _labels(*[(s, 2, 2, 2) for s in human_scores])
    judge = _labels(*[(s, 2, 2, 2) for s in judge_scores])
    return next(r for r in compare(human, judge) if r.dimension == "groundedness")


def test_perfect_agreement_with_variance_is_kappa_one():
    result = _one_dimension([0, 1, 2, 0, 1, 2], [0, 1, 2, 0, 1, 2])
    assert result.kappa == 1.0
    assert result.weighted_kappa == 1.0
    assert result.gates


def test_a_judge_that_says_the_same_thing_every_time_is_undefined_not_perfect():
    """The trap the whole module exists to avoid: if 80% of outputs are fine, a
    judge that always says "fine" agrees 80% of the time having measured
    nothing. Here both scorers are constant, so there is no variance at all."""
    result = _one_dimension([2, 2, 2, 2, 2], [2, 2, 2, 2, 2])
    assert result.raw_agreement == 1.0
    assert result.kappa is None
    assert result.weighted_kappa is None
    assert not result.gates, "an undefined kappa must never earn gating rights"
    assert "no variance" in result.note


def test_high_raw_agreement_can_still_be_a_weak_kappa():
    """The headline claim: raw agreement flatters. Nine of ten match, but the
    judge almost always says 2, so it has barely beaten chance."""
    human = [2, 2, 2, 2, 2, 2, 2, 2, 2, 0]
    judge = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
    result = _one_dimension(human, judge)
    assert result.raw_agreement == 0.9
    assert result.kappa == 0.0, "matching by always guessing the majority is chance-level"
    assert not result.gates


def test_worse_than_chance_is_negative_not_clamped():
    """A systematically inverted judge is a real, informative outcome."""
    result = _one_dimension([0, 0, 2, 2], [2, 2, 0, 0])
    assert result.kappa is not None and result.kappa < 0


def test_weighted_kappa_forgives_adjacent_disagreement():
    """Ordinal scale: off-by-one is a borderline call, 0-vs-2 is reading the
    output completely differently. Plain kappa cannot tell them apart."""
    adjacent = _one_dimension([2, 2, 1, 1, 0, 0], [2, 1, 1, 2, 0, 1])
    extreme = _one_dimension([2, 2, 1, 1, 0, 0], [0, 0, 1, 1, 2, 2])
    assert adjacent.weighted_kappa > adjacent.kappa
    assert extreme.weighted_kappa < adjacent.weighted_kappa


def test_only_seeds_scored_by_both_are_compared():
    """A seed one scorer never reached must be excluded, not paired with the
    wrong output — and `n` has to say how many actually contributed."""
    human = _labels((2, 2, 2, 2), (0, 2, 2, 2), (1, 2, 2, 2))
    judge = {"s0": human["s0"], "s2": human["s2"]}
    result = next(r for r in compare(human, judge) if r.dimension == "groundedness")
    assert result.n == 2


def test_no_shared_seeds_is_reported_rather_than_crashing():
    result = next(r for r in compare(_labels((2, 2, 2, 2)), {}) if r.dimension == "groundedness")
    assert result.n == 0
    assert result.weighted_kappa is None
    assert not result.gates


def test_the_gating_floor_is_the_documented_one():
    """RC1-255 gates only on dimensions that cleared this; `docs/judging.md`
    argues for the value rather than treating it as obvious."""
    assert GATING_FLOOR == 0.6
    assert not _one_dimension([2, 2, 2, 0], [2, 2, 0, 2]).gates


def test_the_confusion_table_says_how_they_disagree():
    """A kappa says how much; this says which way — a judge that is
    consistently one point generous needs a different fix from a random one."""
    human = _labels((2, 2, 2, 2), (1, 2, 2, 2), (1, 2, 2, 2))
    judge = _labels((2, 2, 2, 2), (2, 2, 2, 2), (2, 2, 2, 2))
    table = confusion(human, judge, "groundedness")
    assert table[(1, 2)] == 2, "judge scored 2 where the human scored 1, twice"
    assert table[(2, 2)] == 1


def test_every_dimension_is_reported():
    results = compare(_labels((2, 1, 0, 2)), _labels((2, 1, 0, 2)))
    assert [r.dimension for r in results] == list(DIMENSION_KEYS)
