"""Work Breakdown Agent — PRD + team roster -> schema-forced WBS with provenance.

The agent embodies the project's core principle: the LLM *proposes* a structured
breakdown (schema-forced against `ProposedWorkBreakdown`, never free-text
parsing), and Python owns the provenance facts (`agent`/`model`/`timestamp`) and
the downstream validation. The Anthropic client is injectable so the orchestration
can be unit-tested without credentials.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from planner_core import Epic, Provenance, Task, TeamMember, WorkBreakdown

from agents.schema import ProposedProvenance, ProposedWorkBreakdown
from agents.usage import AgentUsage

AGENT_NAME = "work-breakdown"
DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You are a senior technical program manager building a project plan from a PRD.

Decompose the document into a work breakdown structure: a handful of epics and \
the concrete, schedulable tasks under them. For every task, suggest an owner from \
the provided team (by id) and a three-point estimate in working days \
(optimistic <= likely <= pessimistic).

Rules:
- Propose only what the PRD supports. Prefer a smaller, defensible breakdown over \
an exhaustive one.
- Every epic and task MUST carry provenance:
  - reasoning: why you proposed it,
  - source_quote: text copied VERBATIM from the PRD that justifies it \
(copy exactly — do not paraphrase),
  - source_section: the exact PRD heading the quote is under (or null if none),
  - confidence: high when the PRD states it outright, medium/low when you are \
inferring a task a competent TPM would add but the PRD does not spell out.
- owner_id must be an id from the team roster, or null if genuinely unclear. \
Never invent an owner.
- Use short, stable, kebab-case ids (e.g. "epic-data", "task-migrate-users"), and \
reference epics from tasks via epic_id.
- Estimates are in working days and must satisfy optimistic <= likely <= pessimistic.
"""


# The agent only needs `client.messages.parse(...)`; typed loosely so a plain
# fake object with that method can be injected in tests without the SDK.
StructuredClient = Any


def _format_roster(team: Sequence[TeamMember]) -> str:
    if not team:
        return "(no team roster provided)"
    return "\n".join(f"- {m.id}: {m.name}" + (f" — {m.role}" if m.role else "") for m in team)


def build_user_prompt(prd_text: str, team: Sequence[TeamMember]) -> str:
    """Assemble the user turn: the roster, then the PRD to break down."""
    return (
        "TEAM ROSTER (assign owner_id from these ids only):\n"
        f"{_format_roster(team)}\n\n"
        "PRD:\n"
        "-----\n"
        f"{prd_text}\n"
        "-----\n\n"
        "Produce the work breakdown structure for this PRD."
    )


class WorkBreakdownAgent:
    """Reads a PRD, emits a validated `WorkBreakdown` with stamped provenance."""

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

    def run(self, prd_text: str, team: Sequence[TeamMember]) -> WorkBreakdown:
        client = self._client or self._default_client()
        proposal = self._propose(client, prd_text, team)
        return self._stamp(proposal)

    def _default_client(self) -> StructuredClient:
        import anthropic

        return anthropic.Anthropic()

    def _propose(
        self, client: StructuredClient, prd_text: str, team: Sequence[TeamMember]
    ) -> ProposedWorkBreakdown:
        response = client.messages.parse(
            model=self._model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(prd_text, team)}],
            output_format=ProposedWorkBreakdown,
        )
        # Side channel: the return type is unchanged, so no caller breaks.
        self.last_usage = AgentUsage.of(response, self._model)
        return response.parsed_output

    def _stamp(self, proposal: ProposedWorkBreakdown) -> WorkBreakdown:
        """Convert the LLM proposal into canonical models, stamping run facts."""
        ts = self._now or datetime.now(UTC)
        epics = [
            Epic(
                id=e.id,
                name=e.name,
                description=e.description,
                provenance=self._provenance(e.provenance, ts),
            )
            for e in proposal.epics
        ]
        tasks = [
            Task(
                id=t.id,
                name=t.name,
                description=t.description,
                epic_id=t.epic_id,
                owner_id=t.owner_id,
                estimate=t.estimate,
                provenance=self._provenance(t.provenance, ts),
            )
            for t in proposal.tasks
        ]
        return WorkBreakdown(epics=epics, tasks=tasks)

    def _provenance(self, proposed: ProposedProvenance, ts: datetime) -> Provenance:
        return Provenance(
            reasoning=proposed.reasoning,
            source_quote=proposed.source_quote,
            source_section=proposed.source_section,
            confidence=proposed.confidence,
            agent=AGENT_NAME,
            model=self._model,
            timestamp=ts,
        )
