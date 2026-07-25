"""Dependency Agent — infers precedence edges over a WBS, then filters them.

The LLM *proposes* dependency edges (finish-to-start by default) and maps gate
constraints ("legal signoff before client data moves") to explicit predecessor
edges, each justified with a verbatim PRD quote. Python then owns the run-facts
(agent/model/timestamp) and, critically, the structural triage: dangling
references, self-loops, and duplicate edges are dropped by
`planner_core.filter_dependencies` before anything is stamped into the plan.
Cycle detection and the rest of the graph verdict run later, in the CLI's
`build_dependency_report`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from planner_core import (
    Constraint,
    CycleBreak,
    Dependency,
    EdgeRejection,
    Milestone,
    Provenance,
    Task,
    filter_dependencies,
    resolve_cycles,
)

from agents.schema import ProposedDependencies, ProposedDependency, ProposedProvenance

AGENT_NAME = "dependency"
DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You are a senior technical program manager inferring task dependencies for a plan.

You are given the tasks already broken out of a PRD, the plan's milestones, the \
project's constraints, and the PRD itself. Propose the precedence edges between \
tasks — what must finish before what can start.

Rules:
- Default every edge to finish_to_start with lag 0 unless the PRD clearly implies \
otherwise.
- Reference nodes ONLY by the ids in the task and milestone lists. Never invent an \
id. Do not create an edge from a node to itself.
- Link each milestone into the graph with an edge from the task whose completion \
marks it reached (predecessor = task, successor = milestone id), so the scheduler \
can project the milestone's date. A milestone is a zero-duration checkpoint; it is \
normally an edge's successor, not its predecessor.
- Map gate constraints to edges: if a constraint says something must happen before \
other work (e.g. "legal sign-off before any client data moves", "SRE review before \
production cutover"), add the edge(s) that enforce it, pointing the gating task at \
the gated task.
- Every edge MUST carry provenance:
  - reasoning: why this ordering is required,
  - source_quote: text copied VERBATIM from the PRD that justifies it \
(copy exactly — do not paraphrase),
  - source_section: the PRD heading the quote is under (or null),
  - confidence: high when the PRD states the ordering outright, medium/low when \
you are inferring a sensible ordering the PRD does not spell out.
- Do not introduce cycles. Prefer fewer, well-justified edges over many weak ones.
"""


# The agent only needs `client.messages.parse(...)`; typed loosely so a plain
# fake object with that method can be injected in tests without the SDK.
StructuredClient = Any


@dataclass
class DependencyResult:
    """Outcome of a dependency run: the edges that entered the plan, why some were
    refused (structural), and which were removed to break cycles."""

    dependencies: list[Dependency]
    rejections: list[EdgeRejection] = field(default_factory=list)
    cycle_breaks: list[CycleBreak] = field(default_factory=list)


def _format_tasks(tasks: Sequence[Task]) -> str:
    return "\n".join(f"- {t.id}: {t.name}" for t in tasks) or "(no tasks)"


def _format_milestones(milestones: Sequence[Milestone]) -> str:
    if not milestones:
        return "(no milestones)"
    return "\n".join(f"- {m.id}: {m.name}" for m in milestones)


def _format_constraints(constraints: Sequence[Constraint]) -> str:
    if not constraints:
        return "(no constraints)"
    lines = []
    for c in constraints:
        detail = c.gate or c.description
        targets = ", ".join(c.applies_to) if c.applies_to else "unspecified"
        lines.append(f"- {c.id} ({c.type}): {detail} [applies to: {targets}]")
    return "\n".join(lines)


def build_user_prompt(
    prd_text: str,
    tasks: Sequence[Task],
    constraints: Sequence[Constraint],
    milestones: Sequence[Milestone] = (),
) -> str:
    """Assemble the user turn: tasks, milestones, constraints, then the PRD."""
    return (
        "TASKS (reference predecessor_id / successor_id from these ids only):\n"
        f"{_format_tasks(tasks)}\n\n"
        "MILESTONES (link each with an edge from the task that completes it):\n"
        f"{_format_milestones(milestones)}\n\n"
        "CONSTRAINTS (map gates to enforcing edges):\n"
        f"{_format_constraints(constraints)}\n\n"
        "PRD:\n"
        "-----\n"
        f"{prd_text}\n"
        "-----\n\n"
        "Propose the dependency edges for this plan."
    )


class DependencyAgent:
    """Infers precedence edges, then filters structurally-invalid ones out."""

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
        tasks: Sequence[Task],
        constraints: Sequence[Constraint],
        milestones: Sequence[Milestone] = (),
    ) -> DependencyResult:
        client = self._client or self._default_client()
        proposal = self._propose(client, prd_text, tasks, constraints, milestones)

        # Structural triage BEFORE stamping: invalid edges never enter the plan,
        # and filtering first avoids self-loops raising at Dependency construction.
        # Milestone ids are valid endpoints so task -> milestone edges survive.
        endpoint_ids = {t.id for t in tasks} | {m.id for m in milestones}
        accepted, rejections = filter_dependencies(list(proposal.dependencies), endpoint_ids)

        ts = self._now or datetime.now(UTC)
        stamped = [self._to_dependency(edge, index, ts) for index, edge in enumerate(accepted)]

        # Break any cycles deterministically before the edges reach the plan.
        dependencies, cycle_breaks = resolve_cycles(stamped)
        return DependencyResult(
            dependencies=dependencies, rejections=rejections, cycle_breaks=cycle_breaks
        )

    def _default_client(self) -> StructuredClient:
        import anthropic

        return anthropic.Anthropic()

    def _propose(
        self,
        client: StructuredClient,
        prd_text: str,
        tasks: Sequence[Task],
        constraints: Sequence[Constraint],
        milestones: Sequence[Milestone] = (),
    ) -> ProposedDependencies:
        response = client.messages.parse(
            model=self._model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": build_user_prompt(prd_text, tasks, constraints, milestones),
                }
            ],
            output_format=ProposedDependencies,
        )
        return response.parsed_output

    def _to_dependency(self, edge: ProposedDependency, index: int, ts: datetime) -> Dependency:
        return Dependency(
            id=f"dep-{index + 1}",
            predecessor_id=edge.predecessor_id,
            successor_id=edge.successor_id,
            type=edge.type,
            lag=edge.lag,
            provenance=self._provenance(edge.provenance, ts),
        )

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
