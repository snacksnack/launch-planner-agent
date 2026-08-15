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

from enum import IntEnum, StrEnum

#: Bump on any change to a dimension's text or the scale. Labels record the
#: version they were collected under, and calibrating across versions is a
#: category error the loader refuses rather than silently averages.
RUBRIC_VERSION = "status-narrative-v2"


class Score(IntEnum):
    """Ordinal. The distance between scores is meaningful, which is the reason
    agreement is weighted rather than exact-match."""

    FAILS = 0
    PARTIAL = 1
    MEETS = 2


class ScoredBy(StrEnum):
    """Who adjudicates a dimension.

    The v1 rubric had no such distinction and conflated two different questions
    inside `groundedness`: *are the facts right* (checkable) and *does it claim
    anything beyond them* (a judgement). Three of the five human-vs-judge
    disagreements in RC1-250 sat exactly on that seam, and no amount of extra
    labelling would have resolved them — the rubric was asking two questions and
    accepting one answer.
    """

    DETERMINISTIC = "deterministic"
    JUDGE = "judge"


class Dimension:
    """One scored axis, with the text a human and the judge both read."""

    __slots__ = ("key", "question", "meets", "partial", "fails", "scored_by")

    def __init__(
        self,
        key: str,
        question: str,
        meets: str,
        partial: str,
        fails: str,
        scored_by: ScoredBy = ScoredBy.JUDGE,
    ) -> None:
        self.key = key
        self.question = question
        self.meets = meets
        self.partial = partial
        self.fails = fails
        self.scored_by = scored_by

    def as_prompt(self) -> str:
        return (
            f"### {self.key}\n{self.question}\n"
            f"- 2 (MEETS): {self.meets}\n"
            f"- 1 (PARTIAL): {self.partial}\n"
            f"- 0 (FAILS): {self.fails}"
        )


FACTS_CORRECT = Dimension(
    key="facts-correct",
    question=(
        "Does every ticket key, date, name and number in the output appear in the facts? "
        "Nothing about interpretation — only whether the values are the ones given."
    ),
    meets="Every value in the output appears in the facts.",
    partial="(not used — a value is in the facts or it is not)",
    fails="A ticket key, date, name or number appears that is not in the facts.",
    scored_by=ScoredBy.DETERMINISTIC,
)

NO_UNSUPPORTED_CLAIMS = Dimension(
    key="no-unsupported-claims",
    question=(
        "Setting the numbers aside — they are checked separately — does the output assert "
        "anything the facts do not contain? Causes, attributions, sentiment, or a severity "
        "the facts do not state."
    ),
    meets=(
        "Every statement is either a fact from the input or a direct restatement of one. "
        "Naming a value the facts contain, however plainly, is not an unsupported claim."
    ),
    partial=(
        "Adds at least one claim the facts do not contain. **A causal, evaluative or "
        "attributive phrase counts** — 'driven by', 'reflecting', 'due to', 'warrants "
        "attention', 'the team is confident' — even when every number is correct."
    ),
    fails=(
        "Asserts a state the facts contradict, or its unsupported claims carry the "
        "substance of the update rather than decorating it."
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

DIMENSIONS: tuple[Dimension, ...] = (
    FACTS_CORRECT,
    NO_UNSUPPORTED_CLAIMS,
    COMPLETENESS,
    ACTIONABILITY,
    TONE,
)

#: The dimensions a human or a judge actually scores. `facts-correct` is
#: excluded: it is adjudicated by `evals.groundedness`, which is exact, free, and
#: already gating — asking a person to hand-score it would be asking them to be a
#: slower regex.
JUDGED: tuple[Dimension, ...] = tuple(d for d in DIMENSIONS if d.scored_by is ScoredBy.JUDGE)
JUDGED_KEYS: tuple[str, ...] = tuple(d.key for d in JUDGED)
DIMENSION_KEYS: tuple[str, ...] = tuple(d.key for d in DIMENSIONS)


def rubric_text() -> str:
    """The full rubric, as both the human CLI and the judge prompt render it.

    One source: a judge scoring against different words than the human read is
    not a calibration, it is two unrelated measurements.
    """
    return "\n\n".join(d.as_prompt() for d in JUDGED)
