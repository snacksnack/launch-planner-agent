"""Deterministic schedule-risk analysis + RAID validation — the machine half.

Two deterministic pieces stand on either side of the RAID agent:

1. `analyze_schedule_risks` mines the CPM schedule for **schedule facts** — a
   single-owner critical chain, a zero-float critical path, near-critical tasks,
   missed deadlines, tight gates. These are handed to the agent as concrete
   material to turn into articulated risks; this is what makes the RAID output
   schedule-aware rather than a PRD summary.
2. `build_raid_report` validates the agent's RAID log: suggested owners resolve,
   PRD-sourced quotes are verbatim, schedule-sourced items cite real facts/nodes,
   risks are scored, decisions carry a rationale.

Both are pure functions over `planner_core` models — no LLM, fully testable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from planner_core.models import ConstraintType, Plan
from planner_core.provenance import Confidence
from planner_core.raid import RaidItem, RaidType
from planner_core.scheduling import Schedule
from planner_core.validation import Severity, ValidationIssue, normalize_whitespace

# A task is "near-critical" when its total float is at or below this (working days).
_NEAR_CRITICAL_FLOAT = 2.0


class ScheduleFact(BaseModel):
    """A deterministic risk signal read off the schedule, fed to the RAID agent."""

    model_config = ConfigDict(extra="forbid")

    code: str
    statement: str
    entity_ids: list[str] = []
    severity_hint: str = "medium"  # low | medium | high


def analyze_schedule_risks(plan: Plan, schedule: Schedule) -> list[ScheduleFact]:
    """Mine the CPM schedule for concrete risk signals (deterministic)."""
    owner_name = {m.id: m.name for m in plan.team}
    task_name = {t.id: t.name for t in plan.tasks}
    owner_of = {t.id: t.owner_id for t in plan.tasks}
    facts: list[ScheduleFact] = []

    critical_ids = schedule.critical_path_ids

    # 1. Single-owner critical chain: one person owning several critical tasks is a
    #    key-person risk — the AC's flagship example.
    by_owner: dict[str, list[str]] = defaultdict(list)
    for tid in critical_ids:
        owner = owner_of.get(tid)
        if owner is not None:
            by_owner[owner].append(tid)
    for owner, tids in sorted(by_owner.items()):
        if len(tids) >= 2:
            who = owner_name.get(owner, owner)
            facts.append(
                ScheduleFact(
                    code="single-owner-critical-path",
                    statement=(
                        f"The critical path runs through a single owner: {who} owns "
                        f"{len(tids)} of {len(critical_ids)} critical-path tasks."
                    ),
                    entity_ids=[owner, *tids],
                    severity_hint="high" if len(tids) >= 3 else "medium",
                )
            )

    # 2. The critical path itself has zero float — any slip on it moves the launch.
    if critical_ids:
        facts.append(
            ScheduleFact(
                code="zero-float-critical-path",
                statement=(
                    f"The critical path has {len(critical_ids)} zero-float task(s); a slip on "
                    "any of them pushes the projected launch out day-for-day."
                ),
                entity_ids=list(critical_ids),
                severity_hint="medium",
            )
        )

    # 3. Near-critical tasks — small float that could vanish under any slippage.
    near = [
        t.task_id
        for t in schedule.tasks.values()
        if not t.is_critical and 0 < t.total_float <= _NEAR_CRITICAL_FLOAT
    ]
    for tid in sorted(near):
        ff = schedule.tasks[tid].total_float
        facts.append(
            ScheduleFact(
                code="near-critical-task",
                statement=(
                    f"'{task_name.get(tid, tid)}' has only {ff:g} working day(s) of slack — "
                    "near-critical; it joins the critical path under a small slip."
                ),
                entity_ids=[tid],
                severity_hint="low",
            )
        )

    # 4. Missed hard-date deadlines (negative slack) are issues, not just risks.
    for check in schedule.deadline_checks:
        if not check.met:
            facts.append(
                ScheduleFact(
                    code="missed-deadline",
                    statement=(
                        f"'{task_name.get(check.task_id, check.task_id)}' misses the "
                        f"{check.constraint_id} deadline by {abs(check.slack_working_days)} "
                        "working day(s)."
                    ),
                    entity_ids=[check.task_id, check.constraint_id],
                    severity_hint="high",
                )
            )

    # 5. Tight gates — a gate whose gated task has little slack is fragile.
    for con in plan.constraints:
        if con.type is not ConstraintType.GATE:
            continue
        for target in con.applies_to:
            ts = schedule.tasks.get(target)
            if ts is not None and ts.total_float <= _NEAR_CRITICAL_FLOAT:
                facts.append(
                    ScheduleFact(
                        code="tight-gate",
                        statement=(
                            f"Gate {con.id} on '{task_name.get(target, target)}' is tight: "
                            f"the gated work has only {ts.total_float:g} working day(s) of slack."
                        ),
                        entity_ids=[target, con.id],
                        severity_hint="medium",
                    )
                )

    return facts


def format_schedule_facts(facts: list[ScheduleFact]) -> str:
    """Render facts for the agent prompt."""
    if not facts:
        return "(no schedule risks detected)"
    return "\n".join(
        f"- [{f.code}] {f.statement} (cites: {', '.join(f.entity_ids)})" for f in facts
    )


# --- validation ------------------------------------------------------------


@dataclass
class RaidReport:
    """The deterministic verdict on an agent-produced RAID log."""

    item_count: int
    issues: list[ValidationIssue]

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [
            f"RAID log: {self.item_count} item(s)",
            f"  errors:   {len(self.errors)}",
            f"  warnings: {len(self.warnings)}",
        ]
        for issue in (*self.errors, *self.warnings):
            marker = "✗" if issue.severity is Severity.ERROR else "!"
            lines.append(f"  {marker} [{issue.code}] {issue.message}")
        return "\n".join(lines)


def _check_item(
    item: RaidItem, member_ids: set[str], node_ids: set[str], haystack: str
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if item.suggested_owner_id is not None and item.suggested_owner_id not in member_ids:
        issues.append(
            ValidationIssue(
                Severity.ERROR, "unknown-owner",
                f"RAID {item.id!r} suggests owner {item.suggested_owner_id!r}, not in the team",
                item.id,
            )
        )

    ev = item.provenance.evidence
    if ev.kind == "prd":
        if normalize_whitespace(ev.source_quote) not in haystack:
            issues.append(
                ValidationIssue(
                    Severity.WARNING, "unverifiable-quote",
                    f"RAID {item.id!r} cites a quote not found verbatim in the PRD", item.id,
                )
            )
    else:  # schedule evidence
        unknown = [e for e in ev.entity_ids if e not in node_ids and e not in member_ids]
        # entity_ids may also reference constraint ids (e.g. missed-deadline); those
        # aren't in node_ids/member_ids, so only flag when *nothing* resolves.
        if ev.entity_ids and len(unknown) == len(ev.entity_ids):
            issues.append(
                ValidationIssue(
                    Severity.WARNING, "dangling-evidence",
                    f"RAID {item.id!r} schedule evidence cites no known task/owner", item.id,
                )
            )

    if item.type is RaidType.RISK and (item.probability is None or item.impact is None):
        issues.append(
            ValidationIssue(
                Severity.WARNING, "unscored-risk",
                f"risk {item.id!r} has no probability/impact score", item.id,
            )
        )
    if item.type is RaidType.RISK and not item.mitigation:
        issues.append(
            ValidationIssue(
                Severity.WARNING, "no-mitigation", f"risk {item.id!r} has no mitigation", item.id
            )
        )
    if item.type is RaidType.DECISION and not item.rationale:
        issues.append(
            ValidationIssue(
                Severity.WARNING, "no-rationale",
                f"decision {item.id!r} has no rationale", item.id,
            )
        )
    if item.provenance.confidence is Confidence.LOW:
        issues.append(
            ValidationIssue(
                Severity.WARNING, "low-confidence",
                f"RAID {item.id!r} was proposed with low confidence", item.id,
            )
        )
    return issues


def build_raid_report(plan: Plan, source_text: str) -> RaidReport:
    """Validate the plan's RAID log deterministically."""
    member_ids = {m.id for m in plan.team}
    node_ids = {t.id for t in plan.tasks} | {m.id for m in plan.milestones}
    haystack = normalize_whitespace(source_text)

    issues: list[ValidationIssue] = []
    seen: set[str] = set()
    for item in plan.raid:
        if item.id in seen:
            issues.append(
                ValidationIssue(
                    Severity.ERROR, "duplicate-id", f"duplicate RAID id {item.id!r}", item.id
                )
            )
        seen.add(item.id)
        issues.extend(_check_item(item, member_ids, node_ids, haystack))

    return RaidReport(item_count=len(plan.raid), issues=issues)
