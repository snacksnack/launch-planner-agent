"""Schema-forcing targets for the LLM — a deliberately reduced proposal shape.

The agent asks the model to fill only the parts of provenance it can actually
know from the document: the *reasoning*, the *verbatim quote*, the *section*, and
its *confidence*. The `agent`, `model`, and `timestamp` fields are facts about
the run, not the document — the Python side stamps those after parsing (see
`work_breakdown.py`), so the model can't hallucinate them.

Every field is required (optionals typed `X | None`, no defaults) so the schema
is friendly to strict structured-output decoding while still allowing nulls.
"""

from __future__ import annotations

from planner_core import Confidence, DependencyType, Evidence, RaidType, ThreePointEstimate
from planner_core.spec_gate import FindingCategory, SpecSeverity
from pydantic import BaseModel, ConfigDict


class ProposedProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning: str
    source_quote: str
    source_section: str | None
    confidence: Confidence


class ProposedEpic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None
    provenance: ProposedProvenance


class ProposedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None
    epic_id: str | None
    owner_id: str | None
    estimate: ThreePointEstimate
    provenance: ProposedProvenance


class ProposedWorkBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epics: list[ProposedEpic]
    tasks: list[ProposedTask]


class ProposedDependency(BaseModel):
    """A proposed precedence edge. Permissive on purpose — structurally invalid
    edges (self-loops, dangling references, duplicates) are filtered and reported
    by `planner_core.filter_dependencies` rather than raising during decode."""

    model_config = ConfigDict(extra="forbid")

    predecessor_id: str
    successor_id: str
    type: DependencyType
    lag: float
    provenance: ProposedProvenance


class ProposedDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependencies: list[ProposedDependency]


class ProposedRaidProvenance(BaseModel):
    """RAID provenance the model fills: reasoning, confidence, and the source
    evidence (a PRD quote *or* a schedule fact). Run facts are stamped by Python."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str
    confidence: Confidence
    evidence: Evidence  # discriminated union: PrdEvidence | ScheduleEvidence


class ProposedRaidItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: RaidType
    title: str
    description: str
    # Risk scoring (null for non-risks). 1..5 scales.
    probability: int | None
    impact: int | None
    mitigation: str | None
    suggested_owner_id: str | None
    # Decision rationale (null for non-decisions; `decided_on` is a human placeholder).
    rationale: str | None
    provenance: ProposedRaidProvenance


class ProposedRaidLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ProposedRaidItem]


class ProposedSpecFinding(BaseModel):
    """A proposed rubric finding (RC1-289). Permissive on purpose — a finding
    whose quote turns out to be whitespace is dropped and counted by the agent
    at stamping time rather than raising during decode, the same posture as
    `ProposedDependency`."""

    model_config = ConfigDict(extra="forbid")

    category: FindingCategory
    severity: SpecSeverity
    suggested_rewrite: str | None
    provenance: ProposedProvenance


class ProposedSpecReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ProposedSpecFinding]
    questions_for_author: list[str]
