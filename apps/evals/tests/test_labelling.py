"""The human labelling loop, driven without a terminal.

Resume is the behaviour worth testing: it is what makes a forty-item set
survivable, and it is exactly the part you cannot check by hand without
labelling forty things twice.
"""

from __future__ import annotations

from evals import labelling
from evals.rubric import DIMENSION_KEYS, RUBRIC_VERSION, Score
from evals.seeds import LabelStore, Seed, unlabelled


def _seed(index: int, variant: str = "agent") -> Seed:
    return Seed(
        id=f"status-narrative-{index:02d}-{variant}",
        subject="status-narrative",
        variant=variant,
        facts={"period_label": f"Week {index}", "health": "green"},
        output={"exec_summary": f"Summary {index}.", "points": [f"Point {index}."]},
        generator_version="test",
    )


class _Script:
    """Answers the prompts in order, and records what was shown."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.shown: list[str] = []

    def read(self, _prompt):
        return self.answers.pop(0) if self.answers else "q"

    def write(self, text):
        self.shown.append(str(text))

    @property
    def transcript(self):
        return "\n".join(self.shown)


def test_a_full_pass_stores_one_label_per_seed(tmp_path):
    store = LabelStore(tmp_path / "labels.jsonl")
    script = _Script(*(["2"] * len(DIMENSION_KEYS) * 2))

    done = labelling.run_session([_seed(0), _seed(1)], store, read=script.read, write=script.write)

    assert done == 2
    labels = store.all()
    assert len(labels) == 2
    assert set(labels[0].scores) == set(DIMENSION_KEYS)
    assert labels[0].rubric_version == RUBRIC_VERSION


def test_quitting_saves_everything_scored_so_far(tmp_path):
    """The resume contract: closing the terminal must never cost a session's
    work, or nobody finishes a forty-item set."""
    store = LabelStore(tmp_path / "labels.jsonl")
    script = _Script(*(["2"] * len(DIMENSION_KEYS)), "q")

    labelling.run_session([_seed(0), _seed(1)], store, read=script.read, write=script.write)

    assert [label.seed_id for label in store.all()] == [_seed(0).id]


def test_a_second_session_only_offers_what_is_left(tmp_path):
    store = LabelStore(tmp_path / "labels.jsonl")
    seeds = [_seed(0), _seed(1), _seed(2)]
    labelling.run_session(
        seeds[:1], store, read=_Script(*(["2"] * len(DIMENSION_KEYS))).read, write=lambda _: None
    )

    remaining = unlabelled(seeds, store.by_scorer(labelling.HUMAN))
    assert [seed.id for seed in remaining] == [seeds[1].id, seeds[2].id]


def test_a_partial_answer_is_never_stored(tmp_path):
    """A missing dimension would silently shrink that dimension's n — better to
    drop the seed and let it come round again."""
    store = LabelStore(tmp_path / "labels.jsonl")
    script = _Script("2", "s")

    labelling.run_session([_seed(0)], store, read=script.read, write=script.write)

    assert store.all() == []
    assert "skipped" in script.transcript


def test_a_typo_re_prompts_rather_than_scoring(tmp_path):
    """Everything downstream is these keystrokes; a stray character must not
    become a score."""
    store = LabelStore(tmp_path / "labels.jsonl")
    script = _Script("x", "9", "2", "2", "2", "2")

    labelling.run_session([_seed(0)], store, read=script.read, write=script.write)

    assert "not a score" in script.transcript
    assert store.all()[0].scores["groundedness"] == Score.MEETS


def test_the_labeller_is_shown_the_facts_and_never_the_variant(tmp_path):
    """Groundedness is unscoreable without the facts. And a labeller who knew an
    output came from the degraded prompt would score it low for that reason —
    calibrating the judge against a hint the judge never gets."""
    store = LabelStore(tmp_path / "labels.jsonl")
    script = _Script(*(["2"] * len(DIMENSION_KEYS)))

    labelling.run_session([_seed(0, "degraded")], store, read=script.read, write=script.write)

    assert "Week 0" in script.transcript, "facts must be on screen"
    assert "Summary 0." in script.transcript
    # No carve-out for the seed id. The first version of this test stripped the
    # id before checking, which hid the fact that the id itself names the
    # variant — the leak only showed up when a real seed was rendered and read.
    assert "degraded" not in script.transcript
    assert _seed(0, "degraded").id not in script.transcript


def test_the_order_separates_variants_of_the_same_facts(tmp_path):
    """Scoring `fallback` immediately after `agent` on identical facts invites
    relative grading; the rubric is absolute."""
    seeds = [_seed(i, v) for i in range(4) for v in ("agent", "fallback", "degraded")]
    order = [seed.id for seed in labelling.shuffled(seeds)]

    adjacent_same_facts = sum(
        1
        for a, b in zip(order, order[1:], strict=False)
        if a[: -len("agent")] == b[: -len("agent")]
    )
    assert adjacent_same_facts < len(seeds) // 2
    assert labelling.shuffled(seeds) == labelling.shuffled(seeds), "must be deterministic"


def test_an_empty_queue_says_so_rather_than_hanging(tmp_path):
    script = _Script()
    done = labelling.run_session(
        [], LabelStore(tmp_path / "l.jsonl"), read=script.read, write=script.write
    )
    assert done == 0
    assert "Nothing left to label" in script.transcript
