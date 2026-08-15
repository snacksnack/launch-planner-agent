"""Run records — what was measured, of which version, at what cost.

A score with no version attached is a number you cannot act on: quality dropped,
and there is no way to tell whether the model changed, the prompt changed, or
the case set did. So every run carries a `SubjectVersion`, and every case
carries its own `Usage`. RC1-254 builds budgets on these fields and RC1-255
builds the trend view on them; both are cheap precisely because the fields are
recorded from the first run rather than retrofitted.

Cost is `Decimal`, not `float`. Per-case costs are fractions of a cent and get
summed across hundreds of cases; binary floating point is the wrong tool for
money even at this scale, and the JSON round-trip keeps the exact string.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DuplicateRunId(Exception):
    """A run id already present in the log.

    Raised rather than tolerated: `evals report <run-id>` resolves by id, so two
    records sharing one would make the report silently ambiguous.
    """


class Usage(BaseModel):
    """What one case consumed. Zero for a deterministic subject, and recorded
    anyway — "this costs nothing" is a finding worth being able to prove."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: float = Field(description="Wall-clock for the subject call, not the scoring.")


class SubjectVersion(BaseModel):
    """What produced a run's outputs, in enough detail to attribute a regression.

    `model` and `prompt_version` are explicitly nullable rather than omitted:
    for a deterministic subject the honest value is "there is no model", and a
    missing key would read as "we forgot to record it".
    """

    model_config = ConfigDict(extra="forbid")

    subject: str
    code_version: str = Field(description="Version of the package under test.")
    model: str | None = None
    prompt_version: str | None = None


class CharacteristicResult(BaseModel):
    """One named expectation, and why it landed the way it did.

    `detail` is required on pass and fail alike. A bare `False` tells you
    something regressed; it does not tell you what to fix, and the whole reason
    RC1-249 reports a confusion matrix instead of a pass rate is that the
    *shape* of a failure is the actionable part.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str
    advisory: bool = Field(
        default=False,
        description=(
            "Reported but never gating. RC1-255 requires that dimensions which have not "
            "earned the right to fail a build cannot do so, and that the output says which "
            "is which — a measurement worth watching is not automatically one worth "
            "blocking on."
        ),
    )


class CaseResult(BaseModel):
    """One case's outcome.

    `error` is distinct from a failed characteristic on purpose. A subject that
    raised produced no output to score, which is a different problem with a
    different fix than a subject that answered badly — merging them would let an
    outage read as a quality regression.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    characteristics: list[CharacteristicResult] = Field(default_factory=list)
    usage: Usage
    error: str | None = None
    observations: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured facts a report aggregates across cases, distinct from pass/fail. "
            "A confusion matrix needs to know which wrong tool was chosen, and 'it failed' "
            "does not carry that — see RC1-249."
        ),
    )

    @property
    def passed(self) -> bool:
        """Advisory characteristics are excluded — that is what makes them advisory."""
        return self.error is None and all(c.passed for c in self.characteristics if not c.advisory)

    @property
    def advisory_failures(self) -> list[CharacteristicResult]:
        """Failing-but-not-gating. Worth surfacing in a report; never a build break."""
        return [c for c in self.characteristics if c.advisory and not c.passed]


class RunRecord(BaseModel):
    """One invocation of one subject over its case set."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    subject_version: SubjectVersion
    started_at: datetime
    finished_at: datetime
    results: list[CaseResult] = Field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.error is not None)

    @property
    def total_cost_usd(self) -> Decimal:
        return sum((r.usage.cost_usd for r in self.results), Decimal("0"))

    @property
    def total_latency_ms(self) -> float:
        return sum(r.usage.latency_ms for r in self.results)


def new_run_id(subject: str, when: datetime | None = None) -> str:
    """`<subject>-<UTC timestamp>`, e.g. `health-20260813T174233.481852Z`.

    The timestamp is in the id for the same reason backup keys carry one
    (ADR-0029): the stamp is fixed-width, so lexical order is chronological
    order and "the newest run" is a sort rather than a parse.

    Microsecond precision rather than the second precision backups use, because
    the cadences differ: a backup runs daily, whereas re-running a subject twice
    while iterating on a case file is normal. At second precision that second
    run collides and `RunStore.append` raises — a real failure found by
    `test_a_second_run_appends_rather_than_replacing`. The duplicate guard stays
    as a backstop; this makes tripping it essentially impossible.
    """
    stamp = (when or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{subject}-{stamp}"


class RunStore:
    """Append-only JSONL log of eval runs. One record per line, newest last.

    The same event-log instinct as the plan store (ADR-0012), without the SQLite
    triggers — a text file cannot enforce its own immutability. So the guarantee
    is structural rather than enforced by the storage layer: this class opens
    the file only in append mode and exposes no update or delete, and a test
    asserts that appending leaves every earlier line byte-identical. Anything
    wanting to rewrite history has to go around this class, visibly.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, record: RunRecord) -> None:
        if self.get(record.run_id) is not None:
            raise DuplicateRunId(
                f"run id {record.run_id!r} is already in {self.path} — "
                "two records with one id would make `evals report` ambiguous"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")

    def all(self) -> list[RunRecord]:
        """Every record, oldest first. Blank lines are skipped; a malformed line
        is a hard error, because silently dropping one would understate a trend."""
        if not self.path.exists():
            return []
        records = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(RunRecord.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"{self.path}:{number} is not a valid run record") from exc
        return records

    def get(self, run_id: str) -> RunRecord | None:
        return next((r for r in self.all() if r.run_id == run_id), None)

    def latest(self, subject: str | None = None) -> RunRecord | None:
        """Most recent run, optionally for one subject."""
        candidates = [
            r for r in self.all() if subject is None or r.subject_version.subject == subject
        ]
        return candidates[-1] if candidates else None


def load_jsonl(path: Path) -> list[dict]:
    """Raw lines, for tests and tooling that need to inspect the file as text
    rather than as parsed records."""
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
