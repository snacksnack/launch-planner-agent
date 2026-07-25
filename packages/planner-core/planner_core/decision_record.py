"""The decision record — a durable, serializable audit of how a plan was built.

The system's differentiator is that a plan is an *audit trail, not a black box*.
That only holds if a human can see the decisions the agents made and the
deterministic validation actions taken on top of them — and today many of those
live only in ephemeral CLI stdout (`filter_dependencies` rejections and
`resolve_cycles` cycle-breaks leave no trace in `plan.json`). This module gives
them a home: a Pydantic `DecisionRecord` that serializes alongside a plan and is
persisted onto the immutable snapshot at commit time (RC1-197).

Two kinds of fact live here:

- **Non-recomputable** — `rejected_edges` and `cycle_breaks` are produced while
  the dependency graph is built and the losing edges are then gone from the plan,
  so they can only be captured at run time and carried forward.
- **Recomputable** — the deterministic validation flags (low confidence,
  unverifiable quote, unenforced gate) and PRD `coverage_gaps` are pure functions
  of `plan + PRD`, so they can be rebuilt on demand (e.g. by the API for a plan
  that was never run through the CLI, like the golden).

`build_decision_record` assembles the whole thing from the two existing report
objects, so there is one source of truth for what counts as a decision.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from planner_core.dependencies import CycleBreak, EdgeRejection, build_dependency_report
from planner_core.validation import ValidationIssue, build_report

# Validation codes that can only be judged against the source PRD; without the
# PRD text they'd fire as false positives, so they're dropped from the record.
_SOURCE_DEPENDENT_CODES = frozenset({"unverifiable-quote"})


class RejectedEdge(BaseModel):
    """A proposed dependency edge the deterministic filter refused entry."""

    model_config = ConfigDict(extra="forbid")

    predecessor_id: str
    successor_id: str
    code: str  # dangling-reference | self-loop | duplicate-edge
    reason: str


class CycleBreakEntry(BaseModel):
    """A dependency edge removed to make the graph acyclic, and the cycle it broke."""

    model_config = ConfigDict(extra="forbid")

    removed_edge_id: str
    predecessor_id: str
    successor_id: str
    cycle: list[str]
    reason: str


class FlaggedIssue(BaseModel):
    """A deterministic validation flag raised against the plan."""

    model_config = ConfigDict(extra="forbid")

    severity: str  # error | warning
    code: str
    message: str
    entity_id: str | None = None


class DecisionRecord(BaseModel):
    """A durable audit of the agent decisions and validation actions behind a plan."""

    model_config = ConfigDict(extra="forbid")

    rejected_edges: list[RejectedEdge] = []
    cycle_breaks: list[CycleBreakEntry] = []
    flagged: list[FlaggedIssue] = []
    coverage_gaps: list[str] = []

    @property
    def is_empty(self) -> bool:
        """True when there is nothing for a reviewer to look at."""
        return not (self.rejected_edges or self.cycle_breaks or self.flagged or self.coverage_gaps)


def _to_rejected(edge: EdgeRejection) -> RejectedEdge:
    return RejectedEdge(
        predecessor_id=edge.predecessor_id,
        successor_id=edge.successor_id,
        code=edge.code,
        reason=edge.reason,
    )


def _to_cycle_break(brk: CycleBreak) -> CycleBreakEntry:
    return CycleBreakEntry(
        removed_edge_id=brk.removed_edge_id,
        predecessor_id=brk.predecessor_id,
        successor_id=brk.successor_id,
        cycle=list(brk.cycle),
        reason=brk.reason,
    )


def _to_flagged(issue: ValidationIssue) -> FlaggedIssue:
    return FlaggedIssue(
        severity=issue.severity.value,
        code=issue.code,
        message=issue.message,
        entity_id=issue.entity_id,
    )


def build_decision_record(
    plan,
    source_text: str | None,
    *,
    rejected: list[EdgeRejection] | None = None,
    cycle_breaks: list[CycleBreak] | None = None,
) -> DecisionRecord:
    """Assemble the full decision record for a plan.

    `rejected` / `cycle_breaks` are the run-time facts captured by the Dependency
    Agent (empty for a plan rebuilt from JSON alone). Everything else is derived
    from the two deterministic reports over `plan` (+ `source_text` when the PRD
    is available). Pass `source_text=None`/`""` to skip the checks that can only
    be judged against the PRD rather than emit false positives.
    """
    has_source = bool(source_text)
    src = source_text or ""
    breakdown = build_report(plan, src)
    dependency = build_dependency_report(plan, src, rejected, cycle_breaks)

    issues = [*breakdown.issues, *dependency.issues]
    if not has_source:
        issues = [i for i in issues if i.code not in _SOURCE_DEPENDENT_CODES]
    coverage = breakdown.coverage_gaps if has_source else []

    return DecisionRecord(
        rejected_edges=[_to_rejected(r) for r in dependency.rejected],
        cycle_breaks=[_to_cycle_break(c) for c in dependency.cycle_breaks],
        flagged=[_to_flagged(i) for i in issues],
        coverage_gaps=coverage,
    )
