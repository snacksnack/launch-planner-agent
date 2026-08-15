"""Agreement between two scorers, per dimension.

**Raw agreement is the number not to report.** If 80% of outputs are fine, a
judge that says "fine" to everything agrees with the human 80% of the time while
having measured nothing. Cohen's kappa corrects for exactly that: it asks how
much better than chance the agreement is, given how often each scorer uses each
score.

    kappa = (observed - expected) / (1 - expected)

0 is chance. 1 is perfect. **Negative is worse than chance**, which is a real and
informative outcome, not a bug.

*Weighted, because the scale is ordinal.* Plain kappa treats a 0-vs-2
disagreement exactly like a 1-vs-2, though one is a scorer who read the output
completely differently and the other is a borderline call. Linear weights make
the distance count. Both are reported: plain kappa is the conservative floor,
weighted is the one that reflects what the disagreement actually was.

*Degenerate cases are named, not papered over.* If both scorers give every seed
the same score, the expected agreement is 1, the denominator is 0, and kappa is
undefined. That is not a perfect judge — it is a set with no variance to measure,
which usually means the seed set is too easy. `KappaResult.note` says so, and
`docs/judging.md` explains why a suspiciously good number is a finding to chase.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from evals.rubric import JUDGED_KEYS, Score

#: Below this, a dimension does not gate a build (RC1-255); it is reported as
#: advisory. Landis & Koch call 0.61-0.80 "substantial"; 0.6 is the bottom of
#: that band and is argued for in `docs/judging.md` rather than assumed here.
GATING_FLOOR = 0.6


@dataclass(frozen=True)
class KappaResult:
    """One dimension's agreement, with the context needed to read it."""

    dimension: str
    n: int
    raw_agreement: float
    kappa: float | None
    weighted_kappa: float | None
    note: str = ""

    @property
    def gates(self) -> bool:
        """Whether this dimension has earned the right to fail a build."""
        return self.weighted_kappa is not None and self.weighted_kappa >= GATING_FLOOR

    @property
    def headline(self) -> str:
        if self.weighted_kappa is None:
            return "undefined"
        return f"{self.weighted_kappa:.2f}"


def _expected_agreement(a: list[int], b: list[int], weights) -> float:
    """Chance agreement from each scorer's own score distribution."""
    n = len(a)
    count_a, count_b = Counter(a), Counter(b)
    total = 0.0
    for score_a, freq_a in count_a.items():
        for score_b, freq_b in count_b.items():
            total += (freq_a / n) * (freq_b / n) * weights(score_a, score_b)
    return total


def _observed_agreement(a: list[int], b: list[int], weights) -> float:
    return sum(weights(x, y) for x, y in zip(a, b, strict=True)) / len(a)


def _exact(x: int, y: int) -> float:
    return 1.0 if x == y else 0.0


def _linear(x: int, y: int) -> float:
    """1.0 for a match, falling linearly with distance. On a 0-2 scale an
    adjacent disagreement is worth 0.5 and the extremes are worth 0."""
    span = max(Score) - min(Score)
    return 1.0 - abs(x - y) / span


def _kappa(a: list[int], b: list[int], weights) -> tuple[float | None, str]:
    observed = _observed_agreement(a, b, weights)
    expected = _expected_agreement(a, b, weights)
    if abs(1.0 - expected) < 1e-12:
        return None, (
            "undefined: both scorers used a single score throughout, so there is no "
            "variance to measure — treat as a seed-set problem, not a perfect judge"
        )
    return (observed - expected) / (1.0 - expected), ""


def compare(
    human: dict[str, dict[str, Score]],
    judge: dict[str, dict[str, Score]],
) -> list[KappaResult]:
    """Per-dimension agreement over the seeds both scorers labelled.

    Keyed by seed id, so a seed only one scorer reached is excluded rather than
    silently paired with the wrong output. `n` reports how many actually
    contributed, because a kappa over eight items is not a kappa over forty and
    the report should not let those look alike.
    """
    shared = sorted(set(human) & set(judge))
    results = []
    for dimension in JUDGED_KEYS:
        # Per dimension, not per seed: a dimension-by-dimension labelling pass
        # produces labels that are complete for the dimension being worked on
        # and absent for the rest, and that should calibrate as soon as it is
        # done rather than waiting for all four.
        scored = [
            seed_id
            for seed_id in shared
            if dimension in human[seed_id] and dimension in judge[seed_id]
        ]
        a = [int(human[seed_id][dimension]) for seed_id in scored]
        b = [int(judge[seed_id][dimension]) for seed_id in scored]
        if not a:
            results.append(KappaResult(dimension, 0, 0.0, None, None, "no seeds scored by both"))
            continue
        plain, note = _kappa(a, b, _exact)
        weighted, _ = _kappa(a, b, _linear)
        results.append(
            KappaResult(
                dimension=dimension,
                n=len(a),
                raw_agreement=_observed_agreement(a, b, _exact),
                kappa=plain,
                weighted_kappa=weighted,
                note=note,
            )
        )
    return results


def bootstrap(
    human: dict[str, dict[str, Score]],
    judge: dict[str, dict[str, Score]],
    dimension: str,
    *,
    rounds: int = 4000,
    seed: int = 7,
) -> tuple[float, float, float] | None:
    """`(lo, hi, p_below_floor)` — a 95% interval, and the risk of not gating.

    First-class rather than a script, because RC1-250 turned on exactly this
    number and nearly shipped the wrong conclusion without it. Twelve seeds gave
    kappa 0.82, which clears the floor; doubling to 24 moved it to 0.66 with a
    third of the distribution below. **A point estimate above the floor is not
    the same as having cleared it**, and a report that shows only the point
    estimate invites that mistake every time.

    Seeded, so the same labels always produce the same interval — a confidence
    interval that moves when you re-read it is not evidence.
    """
    import random

    shared = [
        seed_id
        for seed_id in sorted(set(human) & set(judge))
        if dimension in human[seed_id] and dimension in judge[seed_id]
    ]
    if len(shared) < 2:
        return None

    rng = random.Random(seed)
    draws = []
    for _ in range(rounds):
        sample = [rng.choice(shared) for _ in shared]
        left = {f"s{i}": human[x] for i, x in enumerate(sample)}
        right = {f"s{i}": judge[x] for i, x in enumerate(sample)}
        result = next(r for r in compare(left, right) if r.dimension == dimension)
        if result.weighted_kappa is not None:
            draws.append(result.weighted_kappa)
    if not draws:
        return None

    draws.sort()
    lo = draws[int(0.025 * len(draws))]
    hi = draws[min(len(draws) - 1, int(0.975 * len(draws)))]
    below = sum(1 for k in draws if k < GATING_FLOOR) / len(draws)
    return lo, hi, below


def confusion(
    human: dict[str, dict[str, Score]],
    judge: dict[str, dict[str, Score]],
    dimension: str,
) -> dict[tuple[int, int], int]:
    """(human score, judge score) -> count, for one dimension.

    A kappa says how much they disagree; this says *how*. A judge that is
    consistently one point generous is a different fix from one that is random.
    """
    shared = [
        seed_id
        for seed_id in sorted(set(human) & set(judge))
        if dimension in human[seed_id] and dimension in judge[seed_id]
    ]
    pairs = Counter(
        (int(human[seed_id][dimension]), int(judge[seed_id][dimension])) for seed_id in shared
    )
    return dict(pairs)
