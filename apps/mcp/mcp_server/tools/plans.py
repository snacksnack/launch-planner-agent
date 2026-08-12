"""`plan.list` and `plan.get` — the discoverability entry point.

Without these a model has no way to name a plan, so every other plan tool is
unreachable. `plan.list` shows what exists; `plan.get` schedules one and returns
a summary sized for a conversation rather than for a chart.

**On response size.** `build_gantt_payload` is built for a UI: on the flagship
golden it is ~40 KB, of which ~31 KB is the `tasks` array, most of that
provenance blocks carrying verbatim PRD quotes. Pushing that through a tool call
costs a model most of its attention for facts it did not ask for. The default
response here is a summary; `detail=true` returns the full payload for the rare
caller that wants it. `tests/test_plans.py` pins both sizes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.config import get_settings
from app.gantt import build_gantt_payload
from mcp.server import MCPServer
from planner_core import schedule_plan
from pydantic import BaseModel, Field

from mcp_server.errors import InvalidArgument, PlanNotFound, legible_errors
from mcp_server.resolve import ResolvedPlan, resolve_plan_ref, snapshot_history


class PlanRef(BaseModel):
    """Which plan answered, and how to ask for it again.

    Present on every plan-tool response: a plan of record moves, so a date is
    only meaningful alongside the version it was computed from.
    """

    source: str = Field(description="'snapshot' (from the store) or 'file' (the default plan).")
    canonical_ref: str = Field(
        description=(
            "Pass this back as `ref` to target this exact plan again. 'default' means "
            "omit the reference — that plan is a file, not a stored snapshot."
        )
    )
    content_hash: str
    version: int | None = None
    kind: str | None = Field(default=None, description="proposal, commit, or baseline.")
    created_at: datetime | None = None
    approved_by: str | None = None
    message: str | None = None
    path: str | None = Field(default=None, description="Set only when source is 'file'.")

    @classmethod
    def of(cls, resolved: ResolvedPlan) -> PlanRef:
        return cls(
            source=resolved.source,
            canonical_ref=resolved.canonical_ref,
            content_hash=resolved.content_hash,
            version=resolved.version,
            kind=resolved.kind,
            created_at=resolved.created_at,
            approved_by=resolved.approved_by,
            message=resolved.message,
            path=resolved.path,
        )


class SnapshotEntry(BaseModel):
    version: int | None
    kind: str
    content_hash: str = Field(description="Full sha256; a prefix of 4+ chars works as a ref.")
    created_at: datetime
    approved_by: str | None = None
    message: str | None = None


class PlanListing(BaseModel):
    snapshots: list[SnapshotEntry]
    default_plan: PlanRef | None = Field(
        default=None,
        description="The plan used when no reference is given. Null if it is missing.",
    )
    note: str | None = None


class MilestoneSummary(BaseModel):
    id: str
    name: str
    target_date: date | None = None
    projected_date: date | None = None
    slack_working_days: float | None = Field(
        default=None, description="Negative means the projection misses the target."
    )
    scheduled: bool = Field(
        description=(
            "False when no dependency edge reaches this milestone, so it has a target "
            "date but no projection. Absence of a projected date means unlinked, not "
            "on time."
        )
    )


class PlanCounts(BaseModel):
    epics: int
    tasks: int
    dependencies: int
    milestones: int
    constraints: int
    raid_items: int


class PlanSummary(BaseModel):
    ref: PlanRef
    name: str
    counts: PlanCounts
    start_date: date
    launch_date: date | None = Field(description="Projected finish from most-likely estimates.")
    duration_working_days: float
    critical_task_count: int
    meets_all_deadlines: bool
    milestones: list[MilestoneSummary]
    computed_at: datetime
    gantt: dict | None = Field(
        default=None, description="The full UI payload. Only present when detail=true."
    )


def _summarize(resolved: ResolvedPlan, start_date: date, detail: bool) -> PlanSummary:
    plan = resolved.plan
    schedule = schedule_plan(plan, start_date=start_date)
    payload = build_gantt_payload(plan, schedule, jira_base_url=get_settings().jira_base_url)

    milestones = [
        MilestoneSummary(
            id=m["id"],
            name=m["name"],
            target_date=date.fromisoformat(m["target_date"]) if m.get("target_date") else None,
            projected_date=(
                date.fromisoformat(m["projected_date"]) if m.get("projected_date") else None
            ),
            slack_working_days=m.get("slack_working_days"),
            scheduled=bool(m.get("scheduled")),
        )
        for m in payload["milestones"]
    ]

    return PlanSummary(
        ref=PlanRef.of(resolved),
        name=plan.name,
        counts=PlanCounts(
            epics=len(plan.epics),
            tasks=len(plan.tasks),
            dependencies=len(plan.dependencies),
            milestones=len(plan.milestones),
            constraints=len(plan.constraints),
            raid_items=len(plan.raid),
        ),
        start_date=schedule.start_date,
        launch_date=schedule.project_finish_date,
        duration_working_days=schedule.project_duration,
        critical_task_count=len(schedule.critical_path_ids),
        meets_all_deadlines=schedule.meets_all_deadlines,
        milestones=milestones,
        computed_at=datetime.now(UTC),
        gantt=payload if detail else None,
    )


def _start_date(start: str | None) -> date:
    raw = start or get_settings().project_start_date
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidArgument(
            f"{raw!r} is not a valid start date. Use YYYY-MM-DD, or omit it to use "
            f"the configured project start ({get_settings().project_start_date})."
        ) from exc


def register(server: MCPServer) -> None:
    @server.tool(
        name="plan.list",
        description=(
            "List the launch plans available to the other plan tools: every committed "
            "or proposed snapshot in the plan of record, newest last, with its version, "
            "content hash, and who approved it. Start here when you do not already have "
            "a plan reference — the other plan tools need one. Also reports which plan "
            "is used when no reference is given. Takes no arguments and changes nothing."
        ),
    )
    @legible_errors
    def plan_list() -> PlanListing:
        snapshots = [
            SnapshotEntry(
                version=s.version,
                kind=s.kind.value,
                content_hash=s.content_hash,
                created_at=s.created_at,
                approved_by=s.approved_by,
                message=s.message,
            )
            for s in snapshot_history()
        ]
        try:
            default = PlanRef.of(resolve_plan_ref(None))
        except PlanNotFound:
            default = None

        note = None
        if not snapshots:
            note = (
                "The plan store has no snapshots yet — nothing has been committed. "
                "Plan tools called without a reference use the default plan above."
            )
        return PlanListing(snapshots=snapshots, default_plan=default, note=note)

    @server.tool(
        name="plan.get",
        description=(
            "Schedule one launch plan and summarise it: the projected launch date, how "
            "many working days it spans, the milestones with their projected dates and "
            "slack, how many tasks are on the critical path, and whether every hard "
            "deadline is met. Use this to answer 'when does X launch' or 'how big is "
            "this plan'.\n\n"
            "`ref` accepts a version number, a content-hash prefix of 4+ characters, "
            "'latest' (newest commit or baseline), or 'baseline'. Omit it for the "
            "configured default plan. Call plan.list first if you do not have a "
            "reference. `start` overrides the project start date (YYYY-MM-DD).\n\n"
            "Dates come from each task's most-likely estimate, so this is a single "
            "point estimate — use plan.forecast for a confidence band, and "
            "plan.critical_path for which tasks drive the date. Set `detail` only if "
            "you need every task and dependency; the response is far larger. "
            "Read-only: this schedules in memory and changes nothing."
        ),
    )
    @legible_errors
    def plan_get(
        ref: str | None = None,
        start: str | None = None,
        detail: bool = False,
    ) -> PlanSummary:
        return _summarize(resolve_plan_ref(ref), _start_date(start), detail)
