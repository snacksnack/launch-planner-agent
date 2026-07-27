"""Turn a committed plan into Jira issues — mock by default, never a write without approval.

The heart of this module is a single deterministic **generation plan**: a typed
list of issue and link operations derived from the plan + its schedule. Rendering
that list *is* the mock preview; executing it against a `JiraTarget` *is* real
mode. Because both go through the exact same object, "mock matches real 1:1" holds
by construction rather than by discipline.

`planner_core` stays pure: it owns the operation models, the `JiraTarget` port,
the `MockJiraTarget` reference implementation, and the mapping/execution logic —
but does **no** network I/O. The `RealJiraTarget` (an httpx adapter) lives in the
`app` layer, exactly like the SQLite store sits behind the `PlanRepository` port.

Safety: mock is the default everywhere. Idempotency comes from `jira_key` written
back onto each entity — a re-run turns creates into updates, never duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from planner_core.models import Plan
from planner_core.scheduling import Schedule

_TOOL_LABEL = "launch-planner"


# --- operation model (the mock preview == the real-mode input) --------------


class IssueOp(BaseModel):
    """A single issue to create (or update, if it already has a Jira key)."""

    model_config = ConfigDict(extra="forbid")

    local_id: str  # the plan entity id (epic-x / task-y)
    action: str  # "create" | "update"
    issue_type: str  # "Epic" | "Story"
    summary: str
    description: str
    labels: list[str] = Field(default_factory=list)
    due_date: date | None = None
    parent_local_id: str | None = None  # a story's owning epic
    owner_name: str | None = None
    existing_key: str | None = None  # set when action == "update"


class LinkOp(BaseModel):
    """A dependency rendered as a Jira "Blocks" issue link."""

    model_config = ConfigDict(extra="forbid")

    link_type: str = "Blocks"
    outward_local_id: str  # the blocker (predecessor)
    inward_local_id: str  # the blocked (successor)


class GenerationPlan(BaseModel):
    """The full, reviewable set of operations a run would perform."""

    model_config = ConfigDict(extra="forbid")

    project_key: str
    issues: list[IssueOp] = Field(default_factory=list)
    links: list[LinkOp] = Field(default_factory=list)

    @property
    def creates(self) -> int:
        return sum(1 for op in self.issues if op.action == "create")

    @property
    def updates(self) -> int:
        return sum(1 for op in self.issues if op.action == "update")

    def render(self) -> str:
        lines = [
            f"Jira generation plan for project {self.project_key}: "
            f"{self.creates} create, {self.updates} update, {len(self.links)} link(s)",
        ]
        for op in self.issues:
            mark = "+" if op.action == "create" else "~"
            key = f" [{op.existing_key}]" if op.existing_key else ""
            due = f" due {op.due_date.isoformat()}" if op.due_date else ""
            lines.append(f"  {mark} {op.issue_type}{key}: {op.summary}{due}")
        for link in self.links:
            lines.append(f"  → {link.outward_local_id} blocks {link.inward_local_id}")
        return "\n".join(lines)


# --- the port + a credential-free reference implementation ------------------


@runtime_checkable
class JiraTarget(Protocol):
    """Where issues are written. `MockJiraTarget` records; `RealJiraTarget` calls Jira."""

    def create_issue(
        self,
        *,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
        labels: list[str],
        due_date: date | None,
        parent_key: str | None,
    ) -> str:
        """Create an issue and return its key."""
        ...

    def update_issue(
        self,
        key: str,
        *,
        summary: str,
        description: str,
        labels: list[str],
        due_date: date | None,
    ) -> None: ...

    def create_link(self, *, link_type: str, outward_key: str, inward_key: str) -> None: ...


class MockJiraTarget:
    """Records what would be done and hands back fake keys — no side effects.

    The default target: safe for demos, needs no credentials, and is the test
    double the generation logic is verified against.
    """

    def __init__(self, project_key: str = "MOCK") -> None:
        self.project_key = project_key
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.links: list[dict] = []
        self._counter = 0

    def create_issue(
        self,
        *,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
        labels: list[str],
        due_date: date | None,
        parent_key: str | None,
    ) -> str:
        self._counter += 1
        key = f"{project_key}-{self._counter}"
        self.created.append(
            {
                "key": key,
                "issue_type": issue_type,
                "summary": summary,
                "description": description,
                "labels": labels,
                "due_date": due_date.isoformat() if due_date else None,
                "parent_key": parent_key,
            }
        )
        return key

    def update_issue(
        self,
        key: str,
        *,
        summary: str,
        description: str,
        labels: list[str],
        due_date: date | None,
    ) -> None:
        self.updated.append({"key": key, "summary": summary, "labels": labels})

    def create_link(self, *, link_type: str, outward_key: str, inward_key: str) -> None:
        self.links.append(
            {"link_type": link_type, "outward_key": outward_key, "inward_key": inward_key}
        )


# --- building the generation plan (deterministic mapping) -------------------


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def _issue_description(body: str | None, provenance, extra: list[str]) -> str:
    """The issue body plus the provenance block — the audit travels into Jira."""
    lines = [body.strip()] if body else []
    if extra:
        lines.append("")
        lines.extend(extra)
    p = provenance
    lines.extend(
        [
            "",
            "— Generated by launch-planner —",
            f"Reasoning: {p.reasoning}",
            f'Source: "{p.source_quote}" (confidence: {p.confidence.value})',
        ]
    )
    if p.source_section:
        lines.append(f"Section: {p.source_section}")
    return "\n".join(lines)


def build_generation_plan(
    plan: Plan,
    schedule: Schedule,
    *,
    project_key: str,
    extra_labels: tuple[str, ...] = (),
) -> GenerationPlan:
    """Map a plan + its schedule into the Jira operations that would realize it."""
    owner_name = {m.id: m.name for m in plan.team}
    epic_name = {e.id: e.name for e in plan.epics}
    base_labels = [_TOOL_LABEL, *extra_labels]

    issues: list[IssueOp] = []

    for epic in plan.epics:
        issues.append(
            IssueOp(
                local_id=epic.id,
                action="update" if epic.jira_key else "create",
                issue_type="Epic",
                summary=epic.name,
                description=_issue_description(epic.description, epic.provenance, []),
                labels=base_labels,
                existing_key=epic.jira_key,
            )
        )

    for task in plan.tasks:
        ts = schedule.tasks.get(task.id)
        extra: list[str] = []
        labels = list(base_labels)
        if task.epic_id and task.epic_id in epic_name:
            labels.append(_slug(epic_name[task.epic_id]))
        if ts is not None:
            extra.append(
                f"Schedule: {ts.early_start_date.isoformat()} → "
                f"{ts.early_finish_date.isoformat()} "
                f"(float {ts.total_float:g}{', critical' if ts.is_critical else ''})"
            )
            if ts.is_critical:
                labels.append("critical")
        if task.owner_id and task.owner_id in owner_name:
            extra.append(f"Owner: {owner_name[task.owner_id]}")
        issues.append(
            IssueOp(
                local_id=task.id,
                action="update" if task.jira_key else "create",
                issue_type="Story",
                summary=task.name,
                description=_issue_description(task.description, task.provenance, extra),
                labels=labels,
                due_date=ts.early_finish_date if ts is not None else None,
                parent_local_id=task.epic_id if task.epic_id in epic_name else None,
                owner_name=owner_name.get(task.owner_id),
                existing_key=task.jira_key,
            )
        )

    links = [
        LinkOp(outward_local_id=dep.predecessor_id, inward_local_id=dep.successor_id)
        for dep in plan.dependencies
        # only link edges between issues we're generating (tasks/epics), not milestones
        if dep.predecessor_id in {i.local_id for i in issues}
        and dep.successor_id in {i.local_id for i in issues}
    ]

    return GenerationPlan(project_key=project_key, issues=issues, links=links)


# --- executing the plan against a target ------------------------------------


@dataclass
class ExecutionResult:
    """What happened (or would happen) when a generation plan runs."""

    key_by_local_id: dict[str, str] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    linked: int = 0
    skipped: list[str] = field(default_factory=list)


def execute_generation(
    gen: GenerationPlan,
    target: JiraTarget,
    *,
    only: set[str] | None = None,
) -> ExecutionResult:
    """Drive `target` through the generation plan. `only` (local ids) enables
    partial approval — operations outside the set are skipped. Idempotent: an
    op with an `existing_key` updates rather than creates.

    Epics are processed before stories so a story's parent key exists; links are
    created only when both endpoints resolved to a key.
    """
    result = ExecutionResult()
    key_of: dict[str, str] = {op.local_id: op.existing_key for op in gen.issues if op.existing_key}

    ordered = sorted(gen.issues, key=lambda op: 0 if op.issue_type == "Epic" else 1)
    for op in ordered:
        if only is not None and op.local_id not in only:
            result.skipped.append(op.local_id)
            continue
        if op.action == "update" and op.existing_key:
            target.update_issue(
                op.existing_key,
                summary=op.summary,
                description=op.description,
                labels=op.labels,
                due_date=op.due_date,
            )
            result.updated.append(op.existing_key)
            key_of[op.local_id] = op.existing_key
        else:
            parent_key = key_of.get(op.parent_local_id) if op.parent_local_id else None
            key = target.create_issue(
                project_key=gen.project_key,
                issue_type=op.issue_type,
                summary=op.summary,
                description=op.description,
                labels=op.labels,
                due_date=op.due_date,
                parent_key=parent_key,
            )
            key_of[op.local_id] = key
            result.created.append(key)

    result.key_by_local_id = dict(key_of)

    for link in gen.links:
        out_key = key_of.get(link.outward_local_id)
        in_key = key_of.get(link.inward_local_id)
        if out_key and in_key:
            target.create_link(link_type=link.link_type, outward_key=out_key, inward_key=in_key)
            result.linked += 1

    return result


def apply_keys_to_plan(plan: Plan, key_by_local_id: dict[str, str]) -> Plan:
    """Return a copy of the plan with `jira_key` written onto mapped epics/tasks
    (the idempotency record — a re-run then updates instead of duplicating)."""
    updated = plan.model_copy(deep=True)
    for epic in updated.epics:
        if epic.id in key_by_local_id:
            epic.jira_key = key_by_local_id[epic.id]
    for task in updated.tasks:
        if task.id in key_by_local_id:
            task.jira_key = key_by_local_id[task.id]
    return updated
