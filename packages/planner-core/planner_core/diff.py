"""Structured diff between two plans — the human-vs-agent audit trail.

When a person reviews an agent's proposal and edits it, the delta *is* the
record of what the human changed: an overridden estimate, a reassigned owner, a
rejected (removed) dependency, an added edge. `diff_plans(proposed, reviewed)`
computes that delta as structured data so it can be stored, rendered, and queried
later — showing exactly where human judgment diverged from the agents.

Tasks/epics/milestones are matched by id; dependencies are matched by their
(predecessor, successor) pair, since that — not the generated edge id — is an
edge's real identity (a rejected edge is a removed pair).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from planner_core.models import Plan


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: object
    after: object


@dataclass(frozen=True)
class EntityDiff:
    kind: str  # "task" | "dependency" | "epic" | "milestone"
    key: str  # id, or "pred -> succ" for a dependency
    change: str  # "added" | "removed" | "modified"
    fields: tuple[FieldChange, ...] = ()


@dataclass
class PlanDiff:
    entities: list[EntityDiff] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.entities

    def of_kind(self, kind: str) -> list[EntityDiff]:
        return [e for e in self.entities if e.kind == kind]

    def render(self) -> str:
        if self.is_empty:
            return "No changes from the agent proposal."
        symbol = {"added": "+", "removed": "-", "modified": "~"}
        lines = [f"{len(self.entities)} change(s) from the agent proposal:"]
        for e in self.entities:
            lines.append(f"  {symbol[e.change]} {e.kind} {e.key}")
            for fc in e.fields:
                lines.append(f"      {fc.field}: {fc.before!r} -> {fc.after!r}")
        return "\n".join(lines)


def _task_fields(task) -> dict[str, object]:
    return {
        "name": task.name,
        "description": task.description,
        "epic_id": task.epic_id,
        "owner_id": task.owner_id,
        "optimistic": task.estimate.optimistic,
        "likely": task.estimate.likely,
        "pessimistic": task.estimate.pessimistic,
    }


def _dep_fields(dep) -> dict[str, object]:
    return {"type": dep.type.value, "lag": dep.lag}


def _diff_by_id(kind: str, base_items, revised_items, field_extractor) -> list[EntityDiff]:
    base = {i.id: i for i in base_items}
    revised = {i.id: i for i in revised_items}
    diffs: list[EntityDiff] = []
    for key in base.keys() - revised.keys():
        diffs.append(EntityDiff(kind, key, "removed"))
    for key in revised.keys() - base.keys():
        diffs.append(EntityDiff(kind, key, "added"))
    for key in sorted(base.keys() & revised.keys()):
        changes = _field_changes(field_extractor(base[key]), field_extractor(revised[key]))
        if changes:
            diffs.append(EntityDiff(kind, key, "modified", tuple(changes)))
    return diffs


def _field_changes(before: dict, after: dict) -> list[FieldChange]:
    return [
        FieldChange(name, before[name], after[name])
        for name in before
        if before[name] != after[name]
    ]


def _diff_dependencies(base_deps, revised_deps) -> list[EntityDiff]:
    def pair_key(dep) -> str:
        return f"{dep.predecessor_id} -> {dep.successor_id}"

    base = {pair_key(d): d for d in base_deps}
    revised = {pair_key(d): d for d in revised_deps}
    diffs: list[EntityDiff] = []
    for key in base.keys() - revised.keys():
        diffs.append(EntityDiff("dependency", key, "removed"))
    for key in revised.keys() - base.keys():
        diffs.append(EntityDiff("dependency", key, "added"))
    for key in sorted(base.keys() & revised.keys()):
        changes = _field_changes(_dep_fields(base[key]), _dep_fields(revised[key]))
        if changes:
            diffs.append(EntityDiff("dependency", key, "modified", tuple(changes)))
    return diffs


def diff_plans(proposed: Plan, reviewed: Plan) -> PlanDiff:
    """Structured delta from an agent proposal to a human-reviewed plan."""
    entities: list[EntityDiff] = []
    entities += _diff_by_id("epic", proposed.epics, reviewed.epics, lambda e: {"name": e.name})
    entities += _diff_by_id("task", proposed.tasks, reviewed.tasks, _task_fields)
    entities += _diff_dependencies(proposed.dependencies, reviewed.dependencies)
    entities += _diff_by_id(
        "milestone",
        proposed.milestones,
        reviewed.milestones,
        lambda m: {"name": m.name, "target_date": m.target_date},
    )
    return PlanDiff(entities=entities)
