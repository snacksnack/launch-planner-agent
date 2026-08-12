"""`plan.critical_path` — what is actually driving the date right now.

Everything here is already computed by `schedule_plan`; this tool selects the
critical structure out of the Gantt payload rather than recomputing anything or
returning the whole thing.

**Chains are plural.** `Schedule.critical_chains` is a list of ordered sequences,
because a schedule can have several converging critical paths — the flagship
golden has two. Reporting one and dropping the rest would be a confident lie
about what drives the date, so the count is explicit in the response and the
chains are never collapsed.

**Order is meaning.** Each chain comes from `nx.all_simple_paths` over the
critical subgraph, so it is already ordered along its own edges. Nothing here
re-sorts it; sorting by id would silently turn a sequence into a set.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.config import get_settings
from app.gantt import build_gantt_payload
from mcp.server import MCPServer
from planner_core import schedule_plan
from pydantic import BaseModel, Field

from mcp_server.errors import InvalidArgument, legible_errors
from mcp_server.resolve import resolve_plan_ref
from mcp_server.schemas import MilestoneSummary, PlanRef, start_date_or_default

#: Tasks with slack at or below this many working days are "near critical" —
#: they become the critical path the moment anything moves.
DEFAULT_NEAR_CRITICAL_THRESHOLD = 2.0
MAX_NEAR_CRITICAL_THRESHOLD = 60.0


class ScheduledTask(BaseModel):
    id: str
    name: str
    owner_id: str | None = None
    owner_name: str | None = None
    start: date
    finish: date
    duration_working_days: float = Field(
        description="The most-likely estimate — what the schedule uses as duration."
    )
    total_float: float = Field(
        description="Working days this task can slip before the launch date moves."
    )
    free_float: float = Field(
        description="Working days it can slip before any successor is affected."
    )
    is_critical: bool

    @classmethod
    def of(cls, task: dict) -> ScheduledTask:
        return cls(
            id=task["id"],
            name=task["name"],
            owner_id=task.get("owner_id"),
            owner_name=task.get("owner_name"),
            start=date.fromisoformat(task["start"]),
            finish=date.fromisoformat(task["end"]),
            duration_working_days=task["estimate"]["likely"],
            total_float=task["total_float"],
            free_float=task["free_float"],
            is_critical=task["is_critical"],
        )


class CriticalChain(BaseModel):
    """One critical path, ordered from first task to last."""

    length: int
    task_ids: list[str] = Field(description="Ordered along the chain, not sorted.")
    tasks: list[ScheduledTask] = Field(description="Same order as task_ids.")
    owners: list[str] = Field(
        description=(
            "Distinct owner names along this chain. A chain running through one "
            "owner is a single-point-of-failure risk."
        )
    )


class DeadlineCheck(BaseModel):
    constraint_id: str
    task_id: str
    deadline: date
    projected_finish_date: date
    slack_working_days: float = Field(description="Negative means the plan misses the date.")
    met: bool


class CriticalPathReport(BaseModel):
    ref: PlanRef
    start_date: date
    launch_date: date | None
    duration_working_days: float
    chain_count: int = Field(
        description="Number of distinct critical chains. More than one means converging paths."
    )
    critical_task_count: int = Field(description="Distinct tasks across all chains.")
    chains: list[CriticalChain]
    milestones: list[MilestoneSummary]
    deadlines: list[DeadlineCheck]
    meets_all_deadlines: bool
    near_critical: list[ScheduledTask] | None = Field(
        default=None, description="Only present when include_near_critical is true."
    )
    near_critical_threshold: float | None = None
    computed_at: datetime


def _near_critical(tasks: list[dict], threshold: float) -> list[ScheduledTask]:
    """Tasks that are not critical today but have almost no slack.

    Strictly `> 0` float: a zero-float task is already on the critical path and
    is reported in `chains`, so including it here would double-count it.
    """
    candidates = [
        task for task in tasks if not task["is_critical"] and 0 < task["total_float"] <= threshold
    ]
    candidates.sort(key=lambda task: (task["total_float"], task["id"]))
    return [ScheduledTask.of(task) for task in candidates]


def register(server: MCPServer) -> None:
    @server.tool(
        name="plan.critical_path",
        description=(
            "Show which tasks are driving a plan's launch date: every critical chain in "
            "order, with each task's owner, dates, duration, and float, plus milestone "
            "slack and any hard-deadline checks. Use this for 'what is driving the "
            "schedule', 'what is on the critical path', or 'who owns the work that "
            "matters'.\n\n"
            "A schedule can have more than one critical chain when paths converge; "
            "`chain_count` says how many and all of them are returned. Float is the "
            "honest measure of risk here: a task with two days of float is not critical "
            "today but becomes so if anything moves. Set `include_near_critical` to see "
            "those, with `near_critical_threshold` in working days.\n\n"
            "This is deterministic — one CPM pass over each task's most-likely estimate, "
            "so the same plan and start date always give the same answer. It is NOT a "
            "probability. For how *often* a task lands on the critical path across "
            "sampled runs, or for a confidence band on the launch date, use "
            "plan.forecast instead: this tool answers 'what drives the date', "
            "plan.forecast answers 'how likely is that date'.\n\n"
            "`ref` accepts a version number, a hash prefix, 'latest', or 'baseline'; "
            "omit it for the default plan, and call plan.list if you need one. "
            "Read-only: this schedules in memory and changes nothing."
        ),
    )
    @legible_errors
    def plan_critical_path(
        ref: str | None = None,
        start: str | None = None,
        include_near_critical: bool = False,
        near_critical_threshold: float = DEFAULT_NEAR_CRITICAL_THRESHOLD,
    ) -> CriticalPathReport:
        if near_critical_threshold <= 0:
            raise InvalidArgument(
                f"near_critical_threshold must be greater than 0 working days "
                f"(got {near_critical_threshold}). Tasks with zero float are already "
                "on the critical path and are reported in `chains`."
            )
        if near_critical_threshold > MAX_NEAR_CRITICAL_THRESHOLD:
            raise InvalidArgument(
                f"near_critical_threshold must be at most "
                f"{MAX_NEAR_CRITICAL_THRESHOLD:g} working days (got "
                f"{near_critical_threshold}). A larger window returns most of the plan, "
                "which is not a useful answer to 'what is at risk'."
            )

        resolved = resolve_plan_ref(ref)
        start_date = start_date_or_default(start)
        schedule = schedule_plan(resolved.plan, start_date=start_date)
        payload = build_gantt_payload(
            resolved.plan, schedule, jira_base_url=get_settings().jira_base_url
        )
        by_id = {task["id"]: task for task in payload["tasks"]}

        chains: list[CriticalChain] = []
        for chain in schedule.critical_chains:
            # Milestones are zero-duration nodes and can appear on a critical
            # chain; only tasks have a Gantt row, so skip anything else rather
            # than failing the whole call.
            tasks = [ScheduledTask.of(by_id[node]) for node in chain if node in by_id]
            owners = list(
                dict.fromkeys(task.owner_name for task in tasks if task.owner_name)
            )
            chains.append(
                CriticalChain(
                    length=len(tasks),
                    task_ids=[task.id for task in tasks],
                    tasks=tasks,
                    owners=owners,
                )
            )

        return CriticalPathReport(
            ref=PlanRef.of(resolved),
            start_date=schedule.start_date,
            launch_date=schedule.project_finish_date,
            duration_working_days=schedule.project_duration,
            chain_count=len(chains),
            critical_task_count=len(schedule.critical_path_ids),
            chains=chains,
            milestones=[MilestoneSummary.of(m) for m in payload["milestones"]],
            deadlines=[
                DeadlineCheck(
                    constraint_id=d["constraint_id"],
                    task_id=d["task_id"],
                    deadline=date.fromisoformat(d["deadline"]),
                    projected_finish_date=date.fromisoformat(d["projected_finish_date"]),
                    slack_working_days=d["slack_working_days"],
                    met=d["met"],
                )
                for d in payload["deadlines"]
            ],
            meets_all_deadlines=schedule.meets_all_deadlines,
            near_critical=(
                _near_critical(payload["tasks"], near_critical_threshold)
                if include_near_critical
                else None
            ),
            near_critical_threshold=(
                near_critical_threshold if include_near_critical else None
            ),
            computed_at=datetime.now(UTC),
        )
