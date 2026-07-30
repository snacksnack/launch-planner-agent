"""Shape a scheduled plan into the JSON the Gantt UI consumes.

This is the contract between the deterministic backend and the frontend, and the
place the audit trail is surfaced: every task and every dependency edge carries
its full provenance block (reasoning + verbatim source quote + confidence), so
the UI's detail panel can show *why* a task or a buried-constraint dependency
exists — not bury it in raw JSON. Pure and fully unit-testable.
"""

from __future__ import annotations

from typing import Any

from planner_core import ConstraintType, Plan, Provenance, Schedule


def _provenance(prov: Provenance) -> dict[str, Any]:
    return {
        "reasoning": prov.reasoning,
        "source_quote": prov.source_quote,
        "source_section": prov.source_section,
        "confidence": prov.confidence.value,
        "agent": prov.agent,
        "model": prov.model,
        "timestamp": prov.timestamp.isoformat(),
    }


def _jira_url(base_url: str | None, key: str | None) -> str | None:
    """The browse URL for a Jira issue, if we have both a key and a base URL."""
    if base_url and key:
        return f"{base_url.rstrip('/')}/browse/{key}"
    return None


def build_gantt_payload(
    plan: Plan, schedule: Schedule, *, jira_base_url: str | None = None
) -> dict[str, Any]:
    """Transform a plan + its CPM schedule into a Gantt-ready payload."""
    epic_names = {e.id: e.name for e in plan.epics}
    owner_names = {m.id: m.name for m in plan.team}
    milestone_meta = {m.id: m for m in plan.milestones}

    # Group incoming edges per successor so a task carries its predecessors
    # (with the edge's provenance — this is where the buried gate surfaces).
    predecessors: dict[str, list[dict[str, Any]]] = {t.id: [] for t in plan.tasks}
    for dep in plan.dependencies:
        if dep.successor_id in predecessors:
            predecessors[dep.successor_id].append(
                {
                    "id": dep.id,
                    "from": dep.predecessor_id,
                    "type": dep.type.value,
                    "lag": dep.lag,
                    "provenance": _provenance(dep.provenance),
                }
            )

    tasks = []
    for task in plan.tasks:
        ts = schedule.tasks[task.id]
        tasks.append(
            {
                "id": task.id,
                "name": task.name,
                "epic_id": task.epic_id,
                "epic_name": epic_names.get(task.epic_id),
                "start": ts.early_start_date.isoformat(),
                "end": ts.early_finish_date.isoformat(),
                "owner_id": task.owner_id,
                "owner_name": owner_names.get(task.owner_id),
                "estimate": {
                    "optimistic": task.estimate.optimistic,
                    "likely": task.estimate.likely,
                    "pessimistic": task.estimate.pessimistic,
                },
                "total_float": ts.total_float,
                "free_float": ts.free_float,
                "is_critical": ts.is_critical,
                "predecessors": predecessors[task.id],
                "provenance": _provenance(task.provenance),
                "jira_key": task.jira_key,
                "jira_url": _jira_url(jira_base_url, task.jira_key),
            }
        )

    milestones = []
    for ms in schedule.milestones:
        meta = milestone_meta.get(ms.milestone_id)
        milestones.append(
            {
                "id": ms.milestone_id,
                "name": meta.name if meta else ms.milestone_id,
                "target_date": ms.target_date.isoformat() if ms.target_date else None,
                "projected_date": ms.projected_date.isoformat() if ms.projected_date else None,
                "slack_working_days": ms.slack_working_days,
                "scheduled": ms.scheduled,
                "provenance": _provenance(meta.provenance) if meta else None,
            }
        )

    deadlines = [
        {
            "constraint_id": check.constraint_id,
            "task_id": check.task_id,
            "deadline": check.deadline.isoformat(),
            "projected_finish_date": check.projected_finish_date.isoformat(),
            "slack_working_days": check.slack_working_days,
            "met": check.met,
        }
        for check in schedule.deadline_checks
    ]

    raid = [
        {
            **item.model_dump(mode="json"),
            "severity": item.severity,
            "suggested_owner_name": owner_names.get(item.suggested_owner_id),
        }
        for item in plan.raid
    ]

    return {
        "project": {
            "name": plan.name,
            "start_date": schedule.start_date.isoformat(),
            "finish_date": (
                schedule.project_finish_date.isoformat()
                if schedule.project_finish_date
                else None
            ),
            "duration_working_days": schedule.project_duration,
            "critical_path_ids": schedule.critical_path_ids,
            "critical_chains": schedule.critical_chains,
            "meets_all_deadlines": schedule.meets_all_deadlines,
        },
        "epics": [
            {
                "id": e.id,
                "name": e.name,
                "jira_key": e.jira_key,
                "jira_url": _jira_url(jira_base_url, e.jira_key),
            }
            for e in plan.epics
        ],
        "tasks": tasks,
        "milestones": milestones,
        "deadlines": deadlines,
        "raid": raid,
        "freezes": [
            {
                "id": con.id,
                "start": con.window_start.isoformat(),
                "end": con.window_end.isoformat(),
                "label": con.gate or con.description,
            }
            for con in plan.constraints
            if con.type is ConstraintType.BLACKOUT
            and con.window_start is not None
            and con.window_end is not None
        ],
    }
