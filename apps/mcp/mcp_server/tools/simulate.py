"""`plan.simulate` — the what-if, in natural language.

*"What if the auth work slips a week?"* — the question the epic exists to answer.

Two hazards shape this module, and both are about a wrong answer that looks
right.

**The silent no-op.** `apply_scenario` never raises: unknown ids, self-loops,
cycle-creating edges and invalid estimates are collected as `warnings` and
skipped, so the recompute always produces a schedule. That is correct for an
interactive engine and dangerous for a tool — a mistyped task name would return
a successful response with an unchanged schedule, which a model reports as "no
impact on the launch date." So every task reference is resolved to a real id
*before* `simulate()` is called, and a reference that cannot be resolved raises
instead of being passed through.

**Absorbed vs not applied.** A slip smaller than a task's total float is fully
absorbed and the launch date does not move — textbook float behaviour, and a
genuinely useful finding. "Nothing was applied" also leaves the date unmoved.
They are identical in the delta and mean opposite things, so the response
carries an explicit `outcome` and never leaves a caller to infer it from a zero.

Slip is measured in **working days**, because `DelayTask.days` is added to the
task's likely estimate, which the CPM engine consumes as duration.
"""

from __future__ import annotations

import difflib
from datetime import UTC, date, datetime
from typing import Literal

from mcp.server import MCPServer
from planner_core import DelayTask, Plan, Scenario, simulate
from pydantic import BaseModel, Field

from mcp_server.errors import (
    AmbiguousTaskRef,
    InvalidArgument,
    TaskNotFound,
    legible_errors,
)
from mcp_server.resolve import resolve_plan_ref
from mcp_server.schemas import PlanRef, start_date_or_default

#: Guard rail on the slip. Large enough for "what if this slips two quarters",
#: small enough that a fat-fingered 5000 is rejected rather than scheduled.
MAX_SLIP_WORKING_DAYS = 250.0

#: Cap on the per-task movement list so a plan-wide reschedule cannot flood the
#: response. The counts stay exact; only the enumeration is trimmed.
MAX_REPORTED_TASK_SHIFTS = 25


class ResolvedTask(BaseModel):
    """Echoed back so the caller sees which task was actually slipped."""

    id: str
    name: str
    owner_name: str | None = None
    matched_on: str = Field(description="'id', 'exact name', or 'partial name'.")


class TaskMovement(BaseModel):
    task_id: str
    task_name: str
    finish_before: date
    finish_after: date
    finish_shift_days: int
    total_float_before: float
    total_float_after: float
    became_critical: bool
    left_critical: bool


class MilestoneMovement(BaseModel):
    milestone_id: str
    milestone_name: str
    projected_before: date | None
    projected_after: date | None
    slack_before: int | None
    slack_after: int | None


class DeadlineFlip(BaseModel):
    constraint_id: str
    task_id: str
    met_before: bool
    met_after: bool
    slack_before: int
    slack_after: int


class SimulationReport(BaseModel):
    ref: PlanRef
    task: ResolvedTask
    slip_working_days: float

    outcome: Literal["launch_moved", "absorbed_by_float", "not_applied"] = Field(
        description=(
            "launch_moved: the launch date changed. absorbed_by_float: the slip fit "
            "inside the task's float and the date held — a real finding. not_applied: "
            "the change was rejected and nothing was simulated; see warnings. The last "
            "two both leave the date unchanged and mean opposite things."
        )
    )
    applied: bool = Field(description="False means the result describes nothing.")
    summary: str = Field(description="One sentence stating the outcome in plain words.")

    launch_before: date | None
    launch_after: date | None
    launch_shift_working_days: int

    float_before_working_days: float = Field(
        description="The task's total float before the slip — how much it could absorb."
    )
    float_after_working_days: float

    critical_path_changed: bool
    critical_joined: list[str] = Field(description="Tasks that became critical.")
    critical_left: list[str] = Field(description="Tasks that stopped being critical.")

    moved_task_count: int
    moved_tasks: list[TaskMovement]
    moved_tasks_truncated: bool

    milestone_shifts: list[MilestoneMovement]
    deadline_flips: list[DeadlineFlip]

    warnings: list[str] = Field(
        description="Non-empty means the scenario did not fully apply. Never ignore."
    )
    computed_at: datetime


def _task_key(task_id: str) -> str:
    """The id as a human would say it: 'task-legal-review' -> 'legal review'.

    Ids in this corpus are short slugs while names are full sentences ("Obtain
    legal sign-off for client data migration"). Someone asking about "the legal
    review" is naming the id, so the id has to be searchable as a phrase or the
    most natural reference misses entirely.
    """
    stem = task_id[len("task-") :] if task_id.lower().startswith("task-") else task_id
    return stem.replace("-", " ").replace("_", " ").lower()


def resolve_task_ref(plan: Plan, ref: str) -> ResolvedTask:
    """Turn a task id or a human phrase into exactly one task, or raise.

    Tiers run from most to least specific, so a caller who names a task exactly
    always beats someone else's substring. Any tier matching several tasks
    returns the candidates rather than guessing — guessing silently answers a
    question about a task nobody asked about.
    """
    needle = ref.strip()
    if not needle:
        raise InvalidArgument("A task id or name is required.")
    lowered = needle.lower()

    def found(task, how: str) -> ResolvedTask:
        return ResolvedTask(id=task.id, name=task.name, matched_on=how)

    def one_of(matches: list, how: str) -> ResolvedTask | None:
        if len(matches) == 1:
            return found(matches[0], how)
        if len(matches) > 1:
            raise AmbiguousTaskRef(needle, [f"{t.name} ({t.id})" for t in matches])
        return None

    tiers = (
        ("id", [t for t in plan.tasks if t.id.lower() == lowered]),
        ("name", [t for t in plan.tasks if t.name.lower() == lowered]),
        ("task key", [t for t in plan.tasks if _task_key(t.id) == lowered]),
        (
            "partial match",
            [
                t
                for t in plan.tasks
                if lowered in t.name.lower() or lowered in _task_key(t.id)
            ],
        ),
    )
    for how, matches in tiers:
        hit = one_of(matches, how)
        if hit is not None:
            return hit

    phrases = [t.name for t in plan.tasks] + [_task_key(t.id) for t in plan.tasks]
    close = difflib.get_close_matches(lowered, phrases, n=3, cutoff=0.5)
    suggestion = f" Did you mean: {'; '.join(close)}?" if close else ""
    raise TaskNotFound(
        f"No task in this plan matches {needle!r}.{suggestion} "
        "Call plan.critical_path, or plan.get with detail=true, to see task names."
    )


def register(server: MCPServer) -> None:
    @server.tool(
        name="plan.simulate",
        description=(
            "Answer 'what if this task slips?' — apply a hypothetical delay to one task, "
            "re-run the critical-path engine, and report what moved: the new launch date, "
            "which tasks shifted, whether the critical path changed, and whether any "
            "deadline flipped.\n\n"
            "`task` is a task id or part of its name ('legal review' works); the response "
            "echoes which task was matched. `days` is the slip in WORKING days, not "
            "calendar days. `ref` and `start` select and schedule the plan as in "
            "plan.get.\n\n"
            "Read `outcome` before reporting anything. A slip smaller than the task's "
            "float is fully absorbed and the launch date does not move — that is a real "
            "finding ('absorbed_by_float'), not an error, and different from 'not_applied', "
            "which means the change was rejected and nothing was simulated. Always relay "
            "any warnings.\n\n"
            "Deterministic: one CPM pass, same answer every time. Nothing is saved — the "
            "scenario is applied to an in-memory copy and discarded, so this changes no "
            "plan and persists no scenario."
        ),
    )
    @legible_errors
    def plan_simulate(
        task: str,
        days: float,
        ref: str | None = None,
        start: str | None = None,
    ) -> SimulationReport:
        if days <= 0:
            raise InvalidArgument(
                f"days must be greater than 0 working days (got {days}). To model a "
                "task finishing early, that is a different change than a slip and is "
                "not supported yet."
            )
        if days > MAX_SLIP_WORKING_DAYS:
            raise InvalidArgument(
                f"days must be at most {MAX_SLIP_WORKING_DAYS:g} working days "
                f"(got {days})."
            )

        resolved = resolve_plan_ref(ref)
        start_date = start_date_or_default(start)
        target = resolve_task_ref(resolved.plan, task)

        result = simulate(
            resolved.plan,
            Scenario(changes=[DelayTask(task_id=target.id, days=days)]),
            start_date=start_date,
        )
        delta = result.delta

        float_before = result.baseline.tasks[target.id].total_float
        float_after = result.simulated.tasks[target.id].total_float

        # `warnings` is the only signal that the change was rejected. The task id
        # is pre-resolved so an unknown id cannot reach here, but anything else
        # the engine skips must not be reported as a clean result.
        applied = not result.warnings
        if not applied:
            outcome = "not_applied"
            summary = (
                f"Nothing was simulated: the {days:g}-working-day slip on "
                f"{target.name!r} was rejected. The launch date below is unchanged "
                "because no change was applied, not because the plan absorbed it."
            )
        elif delta.finish_shift_days:
            direction = "later" if delta.finish_shift_days > 0 else "earlier"
            outcome = "launch_moved"
            summary = (
                f"Slipping {target.name!r} by {days:g} working days moves the launch "
                f"date {abs(delta.finish_shift_days)} working days {direction}, from "
                f"{delta.finish_before} to {delta.finish_after}."
            )
        else:
            outcome = "absorbed_by_float"
            summary = (
                f"Slipping {target.name!r} by {days:g} working days does not move the "
                f"launch date: the task had {float_before:g} working days of float, so "
                "the slip is absorbed. The plan still finishes "
                f"{delta.finish_after}."
            )

        movements = [
            TaskMovement(
                task_id=s.task_id,
                task_name=s.task_name,
                finish_before=s.finish_before,
                finish_after=s.finish_after,
                finish_shift_days=s.finish_shift_days,
                total_float_before=s.total_float_before,
                total_float_after=s.total_float_after,
                became_critical=s.became_critical,
                left_critical=s.left_critical,
            )
            for s in delta.task_shifts
        ]

        return SimulationReport(
            ref=PlanRef.of(resolved),
            task=target,
            slip_working_days=days,
            outcome=outcome,
            applied=applied,
            summary=summary,
            launch_before=delta.finish_before,
            launch_after=delta.finish_after,
            launch_shift_working_days=delta.finish_shift_days,
            float_before_working_days=float_before,
            float_after_working_days=float_after,
            critical_path_changed=bool(delta.critical_joined or delta.critical_left),
            critical_joined=[n.name for n in delta.critical_joined],
            critical_left=[n.name for n in delta.critical_left],
            moved_task_count=len(movements),
            moved_tasks=movements[:MAX_REPORTED_TASK_SHIFTS],
            moved_tasks_truncated=len(movements) > MAX_REPORTED_TASK_SHIFTS,
            milestone_shifts=[
                MilestoneMovement(
                    milestone_id=m.milestone_id,
                    milestone_name=m.milestone_name,
                    projected_before=m.projected_before,
                    projected_after=m.projected_after,
                    slack_before=m.slack_before,
                    slack_after=m.slack_after,
                )
                for m in delta.milestone_shifts
            ],
            deadline_flips=[
                DeadlineFlip(
                    constraint_id=d.constraint_id,
                    task_id=d.task_id,
                    met_before=d.met_before,
                    met_after=d.met_after,
                    slack_before=d.slack_before,
                    slack_after=d.slack_after,
                )
                for d in delta.deadline_flips
            ],
            warnings=result.warnings,
            computed_at=datetime.now(UTC),
        )
