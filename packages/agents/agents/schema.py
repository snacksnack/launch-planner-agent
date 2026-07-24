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

from planner_core import Confidence, ThreePointEstimate
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
