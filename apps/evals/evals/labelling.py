"""The human labelling loop.

Design constraints, in order of how much they matter:

1. **Resumable.** Forty items is more than one sitting. Every score is appended
   the moment it is entered, so quitting loses nothing and restarting picks up
   where it stopped.
2. **The facts are on screen.** Groundedness cannot be scored from memory — a
   labeller has to be able to check a date against the input without leaving.
3. **The rubric is on screen, from the same source the judge reads.** A human
   scoring against remembered wording and a judge scoring against
   `rubric.rubric_text()` is not a calibration.
4. **No blind ordering.** Seeds are shuffled deterministically so the three
   variants of one fact set are not adjacent — scoring `fallback` right after
   `agent` on identical facts invites relative grading, and the rubric is
   absolute.

The variant (`agent` / `fallback` / `degraded`) is deliberately **not** shown. A
labeller who knows an output came from the degraded prompt will score it lower,
and that would calibrate the judge against a hint the judge never gets.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from evals.rubric import JUDGED, RUBRIC_VERSION, Score
from evals.seeds import Label, Seed

HUMAN = "human"

#: The mapping is spelled out *in the prompt itself*, and the options above it
#: are listed ascending to match.
#:
#: The first version listed 2/1/0 descending and then prompted "score 0/1/2"
#: ascending. A careful pass under that prompt scored the rule-written
#: `fallback` variant — which restates the facts verbatim and cannot contain an
#: unsupported claim — 0.00 on groundedness, and the deliberately-degraded
#: variant 1.88. Inverting those scores moved agreement with the judge from
#: -0.35 to +0.27. A scale a careful person can enter backwards is a broken
#: instrument, and every label collected under it is suspect.
_PROMPT = "  score  [0=fails  1=partial  2=meets]  (s=skip, q=save and quit, ?=rubric): "


def shuffled(seeds: list[Seed]) -> list[Seed]:
    """Deterministic shuffle, keyed on seed id.

    Deterministic so two labelling sessions see the same order and the set is
    reproducible; shuffled so the three variants of one fact set are separated.
    """
    return sorted(seeds, key=lambda s: hashlib.sha256(s.id.encode()).hexdigest())


@dataclass
class Prompted:
    """One seed rendered for a labeller — no variant, no generator.

    `reference` is an opaque token, **not** the seed id. Seed ids encode the
    variant (`status-narrative-10-degraded`), so printing one would tell the
    labeller which outputs came from the deliberately-degraded prompt — the
    exact hint the judge never gets, and enough to bias a whole calibration.
    Caught by rendering a real seed and reading the header.
    """

    reference: str
    facts: str
    output: str

    def render(self, index: int, total: int) -> str:
        return (
            f"\n{'─' * 78}\n"
            f"[{index}/{total}]  ref {self.reference}\n"
            f"{'─' * 78}\n"
            f"FACTS (score groundedness against these):\n{self.facts}\n\n"
            f"OUTPUT:\n{self.output}\n"
        )


def reference_for(seed_id: str) -> str:
    """A stable, opaque handle for a seed.

    Stable so a labeller can quote one back and it can be looked up; opaque so
    the header carries no information about how the output was produced.
    """
    return hashlib.sha256(seed_id.encode()).hexdigest()[:8]


def present(seed: Seed) -> Prompted:
    """Render a seed without leaking how it was generated."""
    return Prompted(
        reference=reference_for(seed.id),
        facts=json.dumps(seed.facts, indent=2, sort_keys=True),
        output=seed.rendered_output(),
    )


def label_from_scores(seed_id: str, scores: dict[str, int], note: str = "") -> Label:
    return Label(
        seed_id=seed_id,
        scorer=HUMAN,
        rubric_version=RUBRIC_VERSION,
        scores={key: Score(value) for key, value in scores.items()},
        note=note,
    )


def run_session(
    seeds: list[Seed],
    store,
    *,
    read,
    write,
    dimension=None,
    scorer: str = HUMAN,
    existing: dict | None = None,
) -> int:
    """Walk a labeller through `seeds`, appending each label as it is entered.

    **One dimension at a time when `dimension` is given.** The first pass over
    this set asked for all four scores on every item, which meant switching
    rubric between every keystroke — 144 judgements with maximum context
    switching. Holding one dimension across the whole set keeps its wording in
    working memory, and it is what annotation practice does for exactly this
    reason. The whole-item mode is kept for a quick pass, not a careful one.

    `read`/`write` are injected rather than hard-wired to `input`/`print` so the
    loop is testable without a terminal — the resume behaviour is the part most
    worth a test and the hardest to check by hand.
    """
    total = len(seeds)
    if not total:
        write("Nothing left to label — the set is complete.")
        return 0

    dimensions = [dimension] if dimension else list(JUDGED)
    if dimension:
        write(f"Scoring ONE dimension across {total} seed(s): {dimension.key}")
        write(f"  {dimension.question}\n")
    else:
        write(f"{total} seed(s), all {len(JUDGED)} judged dimensions each.")
    write("Scores are 0 (fails) / 1 (partial) / 2 (meets).")
    write("Every answer is saved as you go; quit any time and rerun to resume.\n")

    done = 0
    for index, seed in enumerate(seeds, start=1):
        write(present(seed).render(index, total))
        scores: dict[str, int] = {}
        quit_requested = False

        for current in dimensions:
            write(f"\n{current.key}: {current.question}")
            # Ascending, to match the prompt. See `_PROMPT`.
            write(f"  0 = FAILS   — {current.fails}")
            write(f"  1 = PARTIAL — {current.partial}")
            write(f"  2 = MEETS   — {current.meets}")
            answer = _ask(read, write, current)
            if answer == "quit":
                quit_requested = True
                break
            if answer == "skip":
                scores = {}
                break
            scores[current.key] = answer

        if quit_requested:
            break
        if len(scores) != len(dimensions):
            # Skipped, or abandoned partway. A partial score set is never
            # stored: it would silently change one dimension's n.
            write("  …skipped, nothing saved for this one.\n")
            continue

        # In single-dimension mode the other three are carried over from an
        # earlier pass if there is one, so a dimension-by-dimension walk builds
        # a complete label rather than three unusable partial ones.
        merged = dict(existing.get(seed.id, {})) if existing else {}
        merged.update(scores)
        if len(merged) != len(JUDGED):
            write("  saved (partial — other dimensions still to do).\n")
        else:
            write("  saved.\n")
        store.append(
            Label(
                seed_id=seed.id,
                scorer=scorer,
                rubric_version=RUBRIC_VERSION,
                scores={k: Score(v) for k, v in merged.items()},
            )
        )
        done += 1

    write(f"\nLabelled {done} of {total} this session.")
    return done


def _ask(read, write, dimension) -> int | str:
    """Read one score, re-prompting until it is valid.

    A typo must not silently become a score — the whole calibration is downstream
    of these keystrokes.
    """
    while True:
        raw = read(_PROMPT).strip().lower()
        if raw in {"q", "quit"}:
            return "quit"
        if raw in {"s", "skip"}:
            return "skip"
        if raw == "?":
            write(dimension.as_prompt())
            continue
        if raw in {"0", "1", "2"}:
            return int(raw)
        write("  not a score — enter 0, 1, 2, s, q, or ?")
