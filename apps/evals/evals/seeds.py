"""The seed set: agent outputs to be scored, and the labels put on them.

Two append-only JSONL files, same instinct as the run log. Seeds are generated
once and committed; labels accumulate as a human works through them, so the CLI
can be closed and resumed without losing a session's work.

**Seeds carry the facts they were written from.** Groundedness is unscoreable
otherwise — a labeller cannot tell an invented date from a real one without the
input in front of them, and neither can the judge. Storing the pair together
also means the set stays scoreable if the generating code changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evals.rubric import JUDGED_KEYS, RUBRIC_VERSION, Score


class Seed(BaseModel):
    """One agent output, with everything needed to score it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    subject: str = Field(description="Which system produced it, e.g. 'status-narrative'.")
    variant: str = Field(
        description=(
            "How it was produced: 'agent', 'fallback', or 'degraded'. Recorded because a "
            "seed set of only good outputs cannot calibrate anything — see `docs/judging.md`."
        )
    )
    facts: dict[str, Any] = Field(
        description="The structured input. Groundedness is scored against this."
    )
    output: dict[str, Any] = Field(description="What the system wrote.")
    generator_version: str = Field(description="Model or code version that produced the output.")

    def rendered_output(self) -> str:
        """The output as a labeller and the judge both see it."""
        summary = self.output.get("exec_summary", "")
        points = self.output.get("points", []) or []
        lines = [summary.strip()]
        lines += [f"- {point}" for point in points]
        return "\n".join(line for line in lines if line)


class Label(BaseModel):
    """One scoring pass over one seed, by a human or by the judge."""

    model_config = ConfigDict(extra="forbid")

    seed_id: str
    scorer: str = Field(description="'human' or a judge prompt version.")
    rubric_version: str = RUBRIC_VERSION
    scores: dict[str, Score]
    note: str = ""

    def score_for(self, dimension: str) -> Score:
        return self.scores[dimension]


class _JsonlStore:
    """Append-only JSONL, like the run log. No update, no delete."""

    def __init__(self, path: Path, model: type[BaseModel]) -> None:
        self.path = Path(path)
        self._model = model

    def append(self, item: BaseModel) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(item.model_dump_json() + "\n")

    def all(self) -> list[Any]:
        if not self.path.exists():
            return []
        out = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(self._model.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(
                    f"{self.path}:{number} is not a valid {self._model.__name__}"
                ) from exc
        return out


class SeedStore(_JsonlStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path, Seed)

    def ids(self) -> list[str]:
        return [seed.id for seed in self.all()]


class LabelStore(_JsonlStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path, Label)

    def by_scorer(self, scorer: str, rubric_version: str = RUBRIC_VERSION) -> dict[str, Label]:
        """Latest label per seed, for one scorer and one rubric version.

        Latest wins because the file is append-only: correcting a label means
        appending a new one, and the earlier attempt stays on the record rather
        than being edited away.

        Labels from a different rubric version are excluded rather than merged —
        a score of 1 under different wording is not the same measurement, and
        averaging across versions would produce a number with no meaning.
        """
        latest: dict[str, Label] = {}
        for label in self.all():
            if label.scorer == scorer and label.rubric_version == rubric_version:
                latest[label.seed_id] = label
        return latest


def unlabelled(seeds: list[Seed], labels: dict[str, Label]) -> list[Seed]:
    """What is left to score. Drives the CLI's resume behaviour."""
    return [seed for seed in seeds if seed.id not in labels]


def parse_scores(raw: dict[str, int]) -> dict[str, Score]:
    """Validate a full set of dimension scores.

    Partial sets are rejected: a missing dimension would silently drop that seed
    from one dimension's agreement calculation and quietly change its n.
    """
    missing = set(JUDGED_KEYS) - set(raw)
    if missing:
        raise ValueError(f"missing score(s) for: {', '.join(sorted(missing))}")
    unknown = set(raw) - set(JUDGED_KEYS)
    if unknown:
        raise ValueError(f"unknown dimension(s): {', '.join(sorted(unknown))}")
    return {key: Score(value) for key, value in raw.items()}


def load_jsonl(path: Path) -> list[dict]:
    """Raw lines, for tests and tooling that inspect the files as text."""
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
