"""Deterministic validation of an agent-produced plan — the "Python validates" half.

The Work Breakdown Agent *proposes* a structured WBS; this module checks it
without any model in the loop: referential integrity (owners and epics resolve),
provenance quality (low confidence, or a `source_quote` that does not actually
appear in the source document), and coverage (PRD sections no proposed entity
cites). Everything here is a pure function over `planner_core` models, so it is
fully testable without credentials — and it lives in the deterministic core, not
the `agents` layer, on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from planner_core.models import Plan
from planner_core.provenance import Confidence

# Canonical implementations moved to `spec_gate.text` when the spec gate became
# their second caller (RC1-286); re-imported here so every existing import path
# — including the package-root export — keeps working unchanged.
from planner_core.spec_gate.text import (
    normalize_for_quote_match,
    normalize_whitespace,
)


class Severity(StrEnum):
    ERROR = "error"  # the plan is malformed and should not be committed as-is
    WARNING = "warning"  # worth a human's eyes before accepting


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    entity_id: str | None = None


# Backwards-compatible internal alias.
_normalize = normalize_whitespace


# --- individual checks -----------------------------------------------------


def check_owners_exist(plan: Plan) -> list[ValidationIssue]:
    """Every assigned owner must be a real member of the team roster."""
    member_ids = {m.id for m in plan.team}
    issues: list[ValidationIssue] = []
    for task in plan.tasks:
        if task.owner_id is not None and task.owner_id not in member_ids:
            issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "unknown-owner",
                    f"task {task.id!r} is owned by {task.owner_id!r}, who is not in the team",
                    task.id,
                )
            )
    return issues


def check_epics_exist(plan: Plan) -> list[ValidationIssue]:
    """Every task's epic_id must resolve to a proposed epic."""
    epic_ids = {e.id for e in plan.epics}
    issues: list[ValidationIssue] = []
    for task in plan.tasks:
        if task.epic_id is not None and task.epic_id not in epic_ids:
            issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "unknown-epic",
                    f"task {task.id!r} references epic {task.epic_id!r}, which was not proposed",
                    task.id,
                )
            )
    return issues


def flag_unassigned_tasks(plan: Plan) -> list[ValidationIssue]:
    """Tasks with no owner — not fatal, but a human should assign one."""
    return [
        ValidationIssue(Severity.WARNING, "unassigned-task", f"task {t.id!r} has no owner", t.id)
        for t in plan.tasks
        if t.owner_id is None
    ]


def flag_low_confidence(plan: Plan) -> list[ValidationIssue]:
    """Surface low-confidence extractions rather than accepting them silently."""
    issues: list[ValidationIssue] = []
    for kind, items in (("epic", plan.epics), ("task", plan.tasks)):
        for item in items:
            if item.provenance.confidence is Confidence.LOW:
                issues.append(
                    ValidationIssue(
                        Severity.WARNING,
                        "low-confidence",
                        f"{kind} {item.id!r} was extracted with low confidence",
                        item.id,
                    )
                )
    return issues


def flag_unverifiable_quotes(plan: Plan, source_text: str) -> list[ValidationIssue]:
    """Flag any source_quote that does not appear verbatim in the source document.

    This is the hallucination guard: provenance is only trustworthy if the quote
    is real. Matching ignores line-wrapping and markdown decoration — see
    `normalize_for_quote_match` for the false positives that bought each of
    those, all found by running the agent rather than by a passing test.
    """
    haystack = normalize_for_quote_match(source_text)
    issues: list[ValidationIssue] = []
    for kind, items in (("epic", plan.epics), ("task", plan.tasks)):
        for item in items:
            quote = normalize_for_quote_match(item.provenance.source_quote)
            if quote not in haystack:
                issues.append(
                    ValidationIssue(
                        Severity.WARNING,
                        "unverifiable-quote",
                        f"{kind} {item.id!r} cites a source_quote not found verbatim in the PRD",
                        item.id,
                    )
                )
    return issues


# --- coverage --------------------------------------------------------------


def markdown_sections(source_text: str) -> list[str]:
    """Extract markdown heading titles (any level) in document order.

    Delegates to the spec-gate section parser (RC1-286) — one parser for the
    coverage report and the spec gate, so the two can never disagree about what
    a section is. The parser is fence-aware where the old regex was not; the
    shipped fixture PRDs contain no fenced headings, so output is unchanged on
    every existing corpus (regression-tested against the old regex).
    """
    from planner_core.spec_gate.ingest import parse_sections

    return [s.heading for s in parse_sections(source_text) if s.heading is not None]


def coverage_gaps(plan: Plan, source_text: str) -> list[str]:
    """PRD sections that no proposed epic or task cites via provenance.source_section.

    A gap doesn't mean the section *should* have produced work — some sections
    are background — but it tells a reviewer where the agent stayed silent.
    """
    cited = {
        _normalize(item.provenance.source_section)
        for items in (plan.epics, plan.tasks)
        for item in items
        if item.provenance.source_section is not None
    }
    return [title for title in markdown_sections(source_text) if _normalize(title) not in cited]


# --- aggregate report ------------------------------------------------------


@dataclass
class BreakdownReport:
    """The full deterministic verdict on a proposed breakdown."""

    epic_count: int
    task_count: int
    issues: list[ValidationIssue] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        """True when nothing rises to an error (warnings are allowed)."""
        return not self.errors

    def render(self) -> str:
        lines = [
            f"Work breakdown: {self.epic_count} epics, {self.task_count} tasks",
            f"  errors:   {len(self.errors)}",
            f"  warnings: {len(self.warnings)}",
        ]
        for issue in (*self.errors, *self.warnings):
            marker = "✗" if issue.severity is Severity.ERROR else "!"
            lines.append(f"  {marker} [{issue.code}] {issue.message}")
        if self.coverage_gaps:
            lines.append(f"  coverage gaps ({len(self.coverage_gaps)} PRD section(s) uncited):")
            lines.extend(f"    - {title}" for title in self.coverage_gaps)
        return "\n".join(lines)


def build_report(plan: Plan, source_text: str) -> BreakdownReport:
    """Run every check and return the aggregated report."""
    issues = [
        *check_owners_exist(plan),
        *check_epics_exist(plan),
        *flag_unassigned_tasks(plan),
        *flag_low_confidence(plan),
        *flag_unverifiable_quotes(plan, source_text),
    ]
    return BreakdownReport(
        epic_count=len(plan.epics),
        task_count=len(plan.tasks),
        issues=issues,
        coverage_gaps=coverage_gaps(plan, source_text),
    )
