"""Deterministic validation of a dependency graph — guards the critical path.

Hallucinated dependencies are the highest-risk failure mode: one bogus edge can
poison the whole schedule. This module is the machine check that stands between
the LLM's proposed edges and the plan. It has two phases:

1. `filter_dependencies` — structural triage that runs *before* edges enter the
   plan. Dangling references, self-loops, and duplicates are dropped with a
   reason; they never make it into a `Plan`.
2. `build_dependency_report` — graph-level verdict over the edges that survived:
   cycle detection (with the offending path), orphan tasks, gate-constraint
   coverage, and the same provenance guards used for the WBS.

Everything here is pure and uses `networkx` for the graph algorithms — zero LLM
dependency, fully testable without credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import networkx as nx

from planner_core.models import ConstraintType, Dependency, Plan
from planner_core.provenance import Confidence
from planner_core.validation import (
    Severity,
    ValidationIssue,
    normalize_whitespace,
)

# Lower rank = weaker justification = preferred victim when breaking a cycle.
_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


class _Edge(Protocol):
    """Structural view shared by a proposed edge and a canonical `Dependency`."""

    predecessor_id: str
    successor_id: str


@dataclass(frozen=True)
class EdgeRejection:
    """A proposed edge that was refused entry to the plan, and why."""

    index: int
    predecessor_id: str
    successor_id: str
    code: str
    reason: str


def filter_dependencies(
    edges: list, task_ids: set[str]
) -> tuple[list, list[EdgeRejection]]:
    """Split proposed edges into (accepted, rejected).

    Rejects — and therefore keeps out of the plan — edges that reference an
    unknown task, loop a task to itself, or duplicate an already-accepted
    (predecessor, successor) pair. Accepted edges are returned unchanged and in
    order, so the caller can stamp/convert them.
    """
    accepted: list = []
    rejected: list[EdgeRejection] = []
    seen: set[tuple[str, str]] = set()

    for index, edge in enumerate(edges):
        pred, succ = edge.predecessor_id, edge.successor_id
        pair = (pred, succ)
        if pred not in task_ids or succ not in task_ids:
            missing = pred if pred not in task_ids else succ
            rejected.append(
                EdgeRejection(
                    index, pred, succ, "dangling-reference",
                    f"references unknown task {missing!r}",
                )
            )
        elif pred == succ:
            rejected.append(
                EdgeRejection(index, pred, succ, "self-loop", "a task cannot depend on itself")
            )
        elif pair in seen:
            rejected.append(
                EdgeRejection(index, pred, succ, "duplicate-edge", "duplicates an earlier edge")
            )
        else:
            seen.add(pair)
            accepted.append(edge)

    return accepted, rejected


def find_cycles(plan: Plan) -> list[list[str]]:
    """Return elementary cycles in the task/dependency graph (empty if acyclic)."""
    graph = nx.DiGraph()
    graph.add_nodes_from(t.id for t in plan.tasks)
    graph.add_edges_from((d.predecessor_id, d.successor_id) for d in plan.dependencies)
    return [cycle for cycle in nx.simple_cycles(graph)]


@dataclass(frozen=True)
class CycleBreak:
    """A dependency edge removed to make the graph acyclic, and the cycle it broke."""

    removed_edge_id: str
    predecessor_id: str
    successor_id: str
    cycle: tuple[str, ...]
    reason: str


def resolve_cycles(dependencies: list[Dependency]) -> tuple[list[Dependency], list[CycleBreak]]:
    """Break every cycle by dropping the lowest-confidence edge that participates.

    A cycle (``a -> b -> c -> a``) has no valid schedule, so it can't be left in
    the plan — but which edge to cut is a judgment call. The heuristic: remove the
    edge the agent was *least* sure about (lowest provenance confidence), breaking
    ties deterministically by edge id. Iterates until the graph is acyclic. Every
    removal is returned as a `CycleBreak` so the CLI can surface it for review —
    nothing is dropped silently.
    """
    kept = list(dependencies)
    removed: list[CycleBreak] = []

    while True:
        graph = nx.DiGraph()
        graph.add_edges_from((d.predecessor_id, d.successor_id) for d in kept)
        try:
            cycle_edges = nx.find_cycle(graph)
        except nx.NetworkXNoCycle:
            break

        cycle_pairs = {(u, v) for (u, v, *_) in cycle_edges}
        cycle_path = tuple(u for (u, v, *_) in cycle_edges)
        candidates = [d for d in kept if (d.predecessor_id, d.successor_id) in cycle_pairs]
        victim = min(candidates, key=lambda d: (_CONFIDENCE_RANK[d.provenance.confidence], d.id))

        kept.remove(victim)
        path = " -> ".join([*cycle_path, cycle_path[0]])
        removed.append(
            CycleBreak(
                removed_edge_id=victim.id,
                predecessor_id=victim.predecessor_id,
                successor_id=victim.successor_id,
                cycle=cycle_path,
                reason=(
                    f"removed lowest-confidence edge "
                    f"({victim.predecessor_id} -> {victim.successor_id}, "
                    f"{victim.provenance.confidence}) to break cycle {path}"
                ),
            )
        )

    return kept, removed


def orphan_tasks(plan: Plan) -> list[str]:
    """Task ids with no incoming or outgoing dependency edge."""
    connected: set[str] = set()
    for dep in plan.dependencies:
        connected.add(dep.predecessor_id)
        connected.add(dep.successor_id)
    return [t.id for t in plan.tasks if t.id not in connected]


def _cycle_issues(plan: Plan) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for cycle in find_cycles(plan):
        path = " -> ".join([*cycle, cycle[0]])
        issues.append(
            ValidationIssue(Severity.ERROR, "dependency-cycle", f"dependency cycle: {path}")
        )
    return issues


def _orphan_issues(plan: Plan) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            Severity.WARNING, "orphan-task", f"task {tid!r} has no dependencies", tid
        )
        for tid in orphan_tasks(plan)
    ]


def check_gate_coverage(plan: Plan) -> list[ValidationIssue]:
    """Warn when a gate constraint's task is not actually enforced by an edge.

    A gate like "SRE review before prod" is only real if the gated task has a
    predecessor. A gated task with no incoming edge means the gate was mapped in
    prose but never wired into the graph.
    """
    task_ids = {t.id for t in plan.tasks}
    gated_successors = {d.successor_id for d in plan.dependencies}
    issues: list[ValidationIssue] = []
    for con in plan.constraints:
        if con.type is not ConstraintType.GATE:
            continue
        for target in con.applies_to:
            if target in task_ids and target not in gated_successors:
                issues.append(
                    ValidationIssue(
                        Severity.WARNING,
                        "unenforced-gate",
                        f"gate {con.id!r} targets task {target!r}, "
                        "but no dependency enforces it",
                        target,
                    )
                )
    return issues


def flag_unverifiable_dependency_quotes(plan: Plan, source_text: str) -> list[ValidationIssue]:
    """Flag dependency edges whose source_quote is not verbatim in the PRD."""
    haystack = normalize_whitespace(source_text)
    issues: list[ValidationIssue] = []
    for dep in plan.dependencies:
        if normalize_whitespace(dep.provenance.source_quote) not in haystack:
            issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    "unverifiable-quote",
                    f"dependency {dep.id!r} cites a quote not found verbatim in the PRD",
                    dep.id,
                )
            )
    return issues


def flag_low_confidence_dependencies(plan: Plan) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            Severity.WARNING,
            "low-confidence",
            f"dependency {dep.id!r} was inferred with low confidence",
            dep.id,
        )
        for dep in plan.dependencies
        if dep.provenance.confidence is Confidence.LOW
    ]


@dataclass
class DependencyReport:
    """The deterministic verdict on a plan's dependency graph."""

    dependency_count: int
    issues: list[ValidationIssue] = field(default_factory=list)
    rejected: list[EdgeRejection] = field(default_factory=list)
    cycle_breaks: list[CycleBreak] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        """True when no cycle or other error survived (warnings allowed)."""
        return not self.errors

    def render(self) -> str:
        lines = [
            f"Dependencies: {self.dependency_count} accepted, {len(self.rejected)} rejected, "
            f"{len(self.cycle_breaks)} cycle-break(s)",
            f"  errors:   {len(self.errors)}",
            f"  warnings: {len(self.warnings)}",
        ]
        for rej in self.rejected:
            lines.append(
                f"  ✗ [{rej.code}] {rej.predecessor_id} -> {rej.successor_id}: {rej.reason}"
            )
        for brk in self.cycle_breaks:
            lines.append(f"  ! [cycle-break] {brk.reason}")
        for issue in (*self.errors, *self.warnings):
            marker = "✗" if issue.severity is Severity.ERROR else "!"
            lines.append(f"  {marker} [{issue.code}] {issue.message}")
        return "\n".join(lines)


def build_dependency_report(
    plan: Plan,
    source_text: str,
    rejected: list[EdgeRejection] | None = None,
    cycle_breaks: list[CycleBreak] | None = None,
) -> DependencyReport:
    """Run every graph-level check and return the aggregated report.

    `plan` is expected to already be acyclic (the agent runs `resolve_cycles`
    before enriching it). The `_cycle_issues` check is a safety net: if a cycle
    somehow survives — e.g. a hand-authored plan loaded directly — it is still
    reported as an error.
    """
    issues = [
        *_cycle_issues(plan),
        *_orphan_issues(plan),
        *check_gate_coverage(plan),
        *flag_unverifiable_dependency_quotes(plan, source_text),
        *flag_low_confidence_dependencies(plan),
    ]
    return DependencyReport(
        dependency_count=len(plan.dependencies),
        issues=issues,
        rejected=rejected or [],
        cycle_breaks=cycle_breaks or [],
    )
