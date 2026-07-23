"""Provenance — the audit trail that makes an agent-proposed plan inspectable.

Every entity an agent extracts from a source document carries a `Provenance`
block: *why* the agent proposed it (`reasoning`), the *verbatim* text it was
drawn from (`source_quote`), where that text lives (`source_section`), how sure
the agent is (`confidence`), and *who* produced it (`agent` + `model` at a
`timestamp`). This is the project's key differentiator — the plan is an audit
trail, not a black box — so provenance is a required field on every
agent-produced entity (see `ProvenancedModel`), not an optional add-on.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Confidence(StrEnum):
    """How confident the agent is that this entity is correct as extracted."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Provenance(BaseModel):
    """Audit metadata attached to every agent-produced entity.

    `source_quote` must be copied verbatim from the input document so a human
    reviewer can trace any proposed task/dependency/constraint straight back to
    the sentence that justified it.
    """

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        ...,
        min_length=1,
        description="Why the agent proposed this entity — its chain of thought, distilled.",
    )
    source_quote: str = Field(
        ...,
        min_length=1,
        description="Verbatim text from the source document that justifies this entity.",
    )
    source_section: str | None = Field(
        None,
        description="Heading/section of the source document the quote was drawn from.",
    )
    confidence: Confidence = Field(
        ...,
        description="Agent's confidence in this extraction: high / medium / low.",
    )
    agent: str = Field(
        ...,
        min_length=1,
        description="Name of the agent that produced this entity (e.g. 'work-breakdown').",
    )
    model: str = Field(
        ...,
        min_length=1,
        description="Model id that generated this entity (e.g. 'claude-sonnet-5').",
    )
    timestamp: datetime = Field(
        ...,
        description="When the agent produced this entity (timezone-aware UTC recommended).",
    )


class ProvenancedModel(BaseModel):
    """Base for every agent-produced entity: provenance is mandatory.

    Because `provenance` has no default, Pydantic rejects any attempt to build a
    subclass instance without it — which is precisely the acceptance criterion
    that *a Plan cannot be constructed with an agent-generated entity missing
    provenance*. Human-supplied entities (e.g. `TeamMember`) deliberately do not
    inherit from this base.
    """

    model_config = ConfigDict(extra="forbid")

    provenance: Provenance = Field(
        ...,
        description="Audit trail linking this entity back to the source document.",
    )
