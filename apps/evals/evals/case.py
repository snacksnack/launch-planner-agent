"""`Case` — one frozen input and the characteristics its output must exhibit.

The load-bearing decision here is what a case does **not** hold: an expected
output. Every subject under RC1-230 is generative or diagnostic, and its output
is legitimately variable — a case that pinned an exact string would fail on a
harmless rewording, and a brittle assertion gets deleted rather than fixed. So a
case names *characteristics* ("the snapshot count is reported", "no ticket key
appears that wasn't in the input") and the subject owns the predicates that
decide them.

`input` is the world the subject runs against, not necessarily an argument list.
For `platform.health` the tool takes no arguments at all, so the input is the
state of the plan store; for the drift digest it will be a fixture payload. That
generality is intentional and is why the field is a free-form mapping rather
than a typed argument model — one typed model per subject is a thing to build in
RC1-252 when there are two subjects to generalise from.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Case(BaseModel):
    """A golden: fixed input, named expectations, immutable.

    Frozen because cases are ground truth. A scorer that could quietly mutate
    the case it is being judged against is a measurement instrument with a
    thumb on the scale.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(description="Stable, unique within a subject. Appears in every report.")
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="The world state or arguments the subject runs against.",
    )
    expect: tuple[str, ...] = Field(
        description="Characteristic names that must hold. The subject owns the predicates."
    )
    tags: tuple[str, ...] = Field(
        default=(),
        description="Free-form labels for slicing a report (e.g. 'edge-case', 'regression').",
    )

    @field_validator("expect")
    @classmethod
    def _at_least_one_expectation(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """A case with nothing to check always passes, which is worse than no
        case at all — it inflates the pass rate while measuring nothing."""
        if not value:
            raise ValueError("a case must expect at least one characteristic")
        if len(set(value)) != len(value):
            raise ValueError("duplicate characteristic names in `expect`")
        return value
