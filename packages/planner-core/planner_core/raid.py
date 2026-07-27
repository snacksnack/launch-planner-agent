"""RAID domain models — Risks, Assumptions, Issues, Decisions with dual-source evidence.

A RAID log is the PM artifact that sits beside the plan: what could go wrong
(risks), what we're taking for granted (assumptions), what's already gone wrong
(issues), and what we've decided (decisions). Here it is *agent-proposed,
Python-validated, human-approved* like everything else — and, crucially, it draws
on two evidence sources:

- **PRD evidence** — a verbatim quote from the source document (a stated
  assumption, an open question, a decision point), exactly like the WBS agent.
- **Schedule evidence** — a structured *schedule fact* mined deterministically
  from the CPM output (a single-owner critical chain, zero float, a missed
  deadline, a tight gate). This is what makes the RAID agent schedule-aware
  rather than a PRD summarizer.

The evidence is a discriminated union so every item is honestly traceable to one
or the other — the acceptance criterion "every item traceable to a source quote
or a schedule fact." These are pure domain models (they import only
`provenance`), so `models.Plan` can hold a `raid` list without an import cycle;
the analysis and validation logic lives in `raid_analysis`.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from planner_core.provenance import Confidence


class RaidType(StrEnum):
    """The four RAID categories."""

    RISK = "risk"
    ASSUMPTION = "assumption"
    ISSUE = "issue"
    DECISION = "decision"


class PrdEvidence(BaseModel):
    """Evidence drawn from the PRD: a verbatim quote and where it sits."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["prd"] = "prd"
    source_quote: str = Field(..., min_length=1, description="Verbatim text from the PRD.")
    source_section: str | None = None


class ScheduleEvidence(BaseModel):
    """Evidence drawn from the computed schedule: a structured fact + the nodes it cites."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["schedule"] = "schedule"
    fact_code: str = Field(..., min_length=1, description="Stable code for the schedule fact.")
    statement: str = Field(..., min_length=1, description="Human-readable schedule fact.")
    entity_ids: list[str] = Field(
        default_factory=list, description="Task/owner/milestone ids cited."
    )


Evidence = Annotated[PrdEvidence | ScheduleEvidence, Field(discriminator="kind")]


class RaidProvenance(BaseModel):
    """Audit block for a RAID item: the agent's judgment plus its evidence.

    Parallels `Provenance`, but the source is `evidence` (PRD quote *or* schedule
    fact) rather than a bare `source_quote`, since a schedule-derived risk has no
    PRD sentence behind it.
    """

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(..., min_length=1)
    confidence: Confidence
    evidence: Evidence
    agent: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    timestamp: datetime


class RaidItem(BaseModel):
    """One entry in the RAID log. Agent-produced, validated, human-approved."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    type: RaidType
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)

    # Risk scoring (present for risks; None otherwise). 1..5 scales.
    probability: int | None = Field(None, ge=1, le=5)
    impact: int | None = Field(None, ge=1, le=5)
    mitigation: str | None = None
    suggested_owner_id: str | None = Field(
        None, description="Id of a TeamMember suggested to own this item."
    )

    # Decision-log fields (present for decisions; placeholders until a human fills them).
    decided_on: date | None = None
    rationale: str | None = None

    provenance: RaidProvenance

    @property
    def severity(self) -> int | None:
        """Risk severity = probability x impact (1..25), or None if unscored."""
        if self.probability is None or self.impact is None:
            return None
        return self.probability * self.impact
