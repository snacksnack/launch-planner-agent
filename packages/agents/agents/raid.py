"""RAID Agent — PRD + computed schedule facts -> a schema-forced RAID log.

The agent embodies the core principle once more: it *proposes* a typed RAID log
(Risks / Assumptions / Issues / Decisions) schema-forced against
`ProposedRaidLog`, and Python owns the run facts and the downstream validation.
What sets it apart from a summarizer is the **schedule-fact feed**: the CLI runs
`analyze_schedule_risks` over the CPM schedule and hands the agent concrete
signals (single-owner critical chain, zero float, tight gates), which the agent
turns into articulated risks with mitigations — each citing the schedule fact it
came from. PRD-derived items cite a verbatim quote instead. The Anthropic client
is injectable so the orchestration is unit-testable without credentials.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from planner_core import (
    RaidItem,
    RaidProvenance,
    ScheduleFact,
    TeamMember,
    format_schedule_facts,
)

from agents.schema import ProposedRaidItem, ProposedRaidLog

AGENT_NAME = "raid"
DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You are a senior technical program manager compiling a RAID log — Risks, \
Assumptions, Issues, and Decisions — for a project.

You are given the PRD, the project team, and a list of SCHEDULE FACTS computed \
deterministically from the critical-path schedule. Produce a concise, credible \
RAID log that a competent TPM would take to a steering review.

Rules:
- Each item has a type (risk | assumption | issue | decision), a short title, and \
a description.
- Every item MUST carry evidence of exactly ONE kind:
  - kind "prd": a source_quote copied VERBATIM from the PRD (copy exactly — do not \
paraphrase) and the section it sits under, for stated assumptions, open questions, \
or decision points.
  - kind "schedule": cite one of the provided SCHEDULE FACTS — copy its fact_code, \
restate its statement, and list the entity ids it references. Turn the important \
schedule facts (especially a single-owner critical path) into articulated risks.
- For RISKS: set probability and impact as integers 1-5, add a concrete \
mitigation, and suggest an owner_id from the team (or null if genuinely unclear).
- For DECISIONS: add a rationale. Leave scoring null for non-risks.
- reasoning: why this belongs in the log. confidence: high when the PRD or a \
schedule fact states it outright, medium/low when inferring.
- Prefer a focused log of the most important items over an exhaustive one. Do not \
invent an owner id or a schedule fact that was not provided.
"""

# The agent only needs `client.messages.parse(...)`; typed loosely so a plain fake
# object with that method can be injected in tests without the SDK.
StructuredClient = Any


def _format_roster(team: Sequence[TeamMember]) -> str:
    if not team:
        return "(no team roster provided)"
    return "\n".join(f"- {m.id}: {m.name}" + (f" — {m.role}" if m.role else "") for m in team)


def build_user_prompt(
    prd_text: str, facts: Sequence[ScheduleFact], team: Sequence[TeamMember]
) -> str:
    """Assemble the user turn: roster, schedule facts, then the PRD."""
    return (
        "TEAM ROSTER (suggest owner_id from these ids only):\n"
        f"{_format_roster(team)}\n\n"
        "SCHEDULE FACTS (cite a fact_code from these for schedule-kind evidence):\n"
        f"{format_schedule_facts(list(facts))}\n\n"
        "PRD:\n"
        "-----\n"
        f"{prd_text}\n"
        "-----\n\n"
        "Produce the RAID log for this project."
    )


class RaidAgent:
    """Reads a PRD + schedule facts, emits RAID items with stamped provenance."""

    def __init__(
        self,
        *,
        model: str | None = None,
        client: StructuredClient | None = None,
        now: datetime | None = None,
    ) -> None:
        self._model = model or os.environ.get("LPA_ANTHROPIC_MODEL", DEFAULT_MODEL)
        self._client = client
        self._now = now  # injectable so tests get a deterministic timestamp

    def run(
        self,
        prd_text: str,
        facts: Sequence[ScheduleFact],
        team: Sequence[TeamMember],
    ) -> list[RaidItem]:
        client = self._client or self._default_client()
        proposal = self._propose(client, prd_text, facts, team)
        ts = self._now or datetime.now(UTC)
        return [self._to_item(item, ts) for item in proposal.items]

    def _default_client(self) -> StructuredClient:
        import anthropic

        return anthropic.Anthropic()

    def _propose(
        self,
        client: StructuredClient,
        prd_text: str,
        facts: Sequence[ScheduleFact],
        team: Sequence[TeamMember],
    ) -> ProposedRaidLog:
        response = client.messages.parse(
            model=self._model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": build_user_prompt(prd_text, facts, team)}
            ],
            output_format=ProposedRaidLog,
        )
        return response.parsed_output

    def _to_item(self, proposed: ProposedRaidItem, ts: datetime) -> RaidItem:
        return RaidItem(
            id=proposed.id,
            type=proposed.type,
            title=proposed.title,
            description=proposed.description,
            probability=proposed.probability,
            impact=proposed.impact,
            mitigation=proposed.mitigation,
            suggested_owner_id=proposed.suggested_owner_id,
            rationale=proposed.rationale,
            provenance=RaidProvenance(
                reasoning=proposed.provenance.reasoning,
                confidence=proposed.provenance.confidence,
                evidence=proposed.provenance.evidence,
                agent=AGENT_NAME,
                model=self._model,
                timestamp=ts,
            ),
        )
