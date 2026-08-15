"""The rubric a human and a judge both score against.

The whole calibration rests on this file. If a dimension is vague, two people —
or the same person on two different days — score it differently, the measured
agreement collapses, and the honest conclusion is "the rubric is bad" rather
than "the judge is bad". So each dimension states what earns each score in terms
of things you can point at in the output, not adjectives.

**Three points, not five.** A five-point scale invites a middle you cannot
define, and disagreement between adjacent points that means nothing. Three
forces the only judgement that matters downstream: does this meet the bar, is it
partway, or does it fail. Ordinal, so a 0-vs-2 disagreement is worse than a
1-vs-2 — which is why `agreement.py` weights it.

Versioned, because a rubric edit invalidates the labels collected under the old
one. `docs/judging.md` says so plainly; this constant is what makes it checkable.
"""

from __future__ import annotations

from enum import IntEnum

#: Bump on any change to a dimension's text or the scale. Labels record the
#: version they were collected under, and calibrating across versions is a
#: category error the loader refuses rather than silently averages.
RUBRIC_VERSION = "status-narrative-v1"


class Score(IntEnum):
    """Ordinal. The distance between scores is meaningful, which is the reason
    agreement is weighted rather than exact-match."""

    FAILS = 0
    PARTIAL = 1
    MEETS = 2


class Dimension:
    """One scored axis, with the text a human and the judge both read."""

    __slots__ = ("key", "question", "meets", "partial", "fails")

    def __init__(self, key: str, question: str, meets: str, partial: str, fails: str) -> None:
        self.key = key
        self.question = question
        self.meets = meets
        self.partial = partial
        self.fails = fails

    def as_prompt(self) -> str:
        return (
            f"### {self.key}\n{self.question}\n"
            f"- 2 (MEETS): {self.meets}\n"
            f"- 1 (PARTIAL): {self.partial}\n"
            f"- 0 (FAILS): {self.fails}"
        )


GROUNDEDNESS = Dimension(
    key="groundedness",
    question=(
        "Is every claim in the narrative supported by the FACTS? "
        "Judge only support, not usefulness — a dull but accurate summary scores 2."
    ),
    meets=(
        "Every number, date, name, and health state appears in the facts, or follows from them."
    ),
    partial=(
        "Broadly faithful, but includes at least one soft claim the facts do not support "
        "(a cause, an attribution, a 'the team has absorbed it')."
    ),
    fails=(
        "Contains a number, date, task name, or health state that is not in the facts, or "
        "contradicts one that is. An invented figure alone is a 0."
    ),
)

COMPLETENESS = Dimension(
    key="completeness",
    question=(
        "Does it cover the facts that matter for this period — the health state, the launch "
        "movement, and the most significant changes? Missing an unimportant detail is not a miss."
    ),
    meets=(
        "States the health, the launch movement, and every high-significance change "
        "(breaches, newly critical work)."
    ),
    partial="Covers the headline but omits a change a reader would need to act on.",
    fails=(
        "Omits the health state or the launch movement, or reads as though nothing "
        "happened when something did."
    ),
)

ACTIONABILITY = Dimension(
    key="actionability",
    question=(
        "Could a reader tell what to do next, or what to ask about, without opening the plan? "
        "Specific nouns and numbers, not exhortations."
    ),
    meets=(
        "Names the specific tasks, deadlines, or risks a reader would follow up on, "
        "with their magnitude."
    ),
    partial="Identifies that something needs attention but not specifically enough to act on.",
    fails="Generic status prose. A reader learns nothing they could do anything with.",
)

TONE = Dimension(
    key="tone",
    question=(
        "Is this the right register for a weekly executive status update — direct, "
        "non-defensive, no filler? Judge the register, not the facts."
    ),
    meets="Reads like a competent TPM wrote it for a busy executive. Leads with the point.",
    partial="Serviceable but padded, hedged, or buried — the point is there but not first.",
    fails=(
        "Wrong register: cheerleading, apologising, jargon-dense, or so terse it reads as "
        "a machine dump."
    ),
)

DIMENSIONS: tuple[Dimension, ...] = (GROUNDEDNESS, COMPLETENESS, ACTIONABILITY, TONE)
DIMENSION_KEYS: tuple[str, ...] = tuple(d.key for d in DIMENSIONS)


def rubric_text() -> str:
    """The full rubric, as both the human CLI and the judge prompt render it.

    One source: a judge scoring against different words than the human read is
    not a calibration, it is two unrelated measurements.
    """
    return "\n\n".join(d.as_prompt() for d in DIMENSIONS)
