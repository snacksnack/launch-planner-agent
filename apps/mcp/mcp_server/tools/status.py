"""`status.draft` — the weekly exec update, drafted and never sent.

Composes what already exists: `compare_versions` diffs a baseline against the
current plan, `assemble_status` turns that into diff-traceable facts with a
rule-decided health signal, and `fallback_narrative` writes the prose. No new
analysis happens here.

Two things this module is careful about.

**The no-baseline case is a sentence, not an empty object.** `/api/status`
returns ``{"baseline": null}`` when nothing has been committed. A model handed an
empty result narrates it as "no changes this week", which is the opposite of the
truth — there is no baseline to compare against, so nothing is known. That
distinction is the same shape as `plan.simulate`'s absorbed-versus-not-applied
and matters for the same reason.

**The narrative's source is labelled.** The prose here is always the
deterministic, rule-written fallback. The LLM narrative is produced by
`cmd_status` in the CLI when a key is set — a gated path that [1/8]'s import
contract forbids this package from reaching, deliberately. The two artifacts read
alike and a consumer cannot otherwise tell them apart, so every response says
which one it got.

Nothing is sent. The tooling renders and previews; delivery was left to a
scheduling concern in ADR-0019 and there is no send path to reach from here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from mcp.server import MCPServer
from planner_core import (
    assemble_status,
    compare_versions,
    fallback_narrative,
    render_markdown,
)
from pydantic import BaseModel, Field

from mcp_server.errors import PlanNotFound, legible_errors
from mcp_server.resolve import resolve_plan_ref
from mcp_server.schemas import PlanRef, start_date_or_default

#: Enumerated lists are trimmed so a large reporting period cannot flood the
#: response. Counts stay exact — only the enumeration is capped.
MAX_ITEMS_PER_LIST = 15


class TaskChange(BaseModel):
    id: str
    name: str
    shift_working_days: int = 0


class MilestoneDriftOut(BaseModel):
    id: str
    name: str
    projected_before: date | None = None
    projected_after: date | None = None
    slack_shift_working_days: int | None = None


class BreachOut(BaseModel):
    constraint_id: str
    task_id: str
    slack_working_days: int = Field(description="Negative means the deadline is missed.")


class RaidChangeOut(BaseModel):
    id: str
    type: str
    title: str
    severity: str


class StatusFactsOut(BaseModel):
    """Deterministic facts. Every field traces to a diff entry or a RAID item."""

    period_label: str
    health: str = Field(description="green, yellow, or red — decided by rule, not by prose.")
    health_reasons: list[str] = Field(
        description="Machine-generated. Why the health signal is what it is."
    )

    launch_before: date | None = None
    launch_after: date | None = None
    launch_shift_working_days: int = 0

    slipped: list[TaskChange] = []
    newly_critical: list[TaskChange] = []
    no_longer_critical: list[TaskChange] = []
    milestone_drift: list[MilestoneDriftOut] = []
    breaches: list[BreachOut] = []
    raid_added: list[RaidChangeOut] = []
    raid_removed: list[RaidChangeOut] = []
    structural_change_count: int = 0
    truncated_lists: list[str] = Field(
        default=[], description="Names of lists trimmed to the reporting cap."
    )


class StatusDraft(BaseModel):
    current: PlanRef
    baseline: PlanRef
    baseline_version: int | None
    baseline_note: str | None
    start_date: date

    facts: StatusFactsOut
    exec_summary: str
    points: list[str]
    markdown: str = Field(description="The update rendered for pasting into an email.")

    narrative_source: Literal["deterministic", "llm"] = Field(
        description=(
            "Always 'deterministic' here: rule-written, credential-free. The LLM "
            "narrative comes from the gated `plan status` CLI, which this server "
            "cannot reach. The two read alike, so the source is always stated."
        )
    )
    sent: Literal[False] = Field(
        default=False,
        description="This tool drafts and never delivers. There is no send path.",
    )
    computed_at: datetime


def _tasks(items) -> list[TaskChange]:
    return [TaskChange(id=i.id, name=i.name, shift_working_days=i.shift_days) for i in items]


def _milestones(items) -> list[MilestoneDriftOut]:
    return [
        MilestoneDriftOut(
            id=i.id,
            name=i.name,
            projected_before=i.projected_before,
            projected_after=i.projected_after,
            slack_shift_working_days=i.slack_shift_days,
        )
        for i in items
    ]


def _breaches(items) -> list[BreachOut]:
    return [
        BreachOut(
            constraint_id=i.constraint_id, task_id=i.task_id, slack_working_days=i.slack_days
        )
        for i in items
    ]


def _raid(items) -> list[RaidChangeOut]:
    return [
        RaidChangeOut(id=i.id, type=i.type, title=i.title, severity=i.severity) for i in items
    ]


def register(server: MCPServer) -> None:
    @server.tool(
        name="status.draft",
        description=(
            "Draft the weekly status update for a plan: how the launch date has moved "
            "against the committed baseline, which tasks slipped or became critical, "
            "which milestones drifted, what RAID items opened or closed, and a "
            "rule-decided health signal with its reasons. Returns both structured facts "
            "and ready-to-paste Markdown.\n\n"
            "Every statement traces to a diff entry — the health signal is decided by "
            "rule, never by prose, so a week with critical-path slippage flips it "
            "regardless of how the summary reads. The narrative is the deterministic, "
            "rule-written one; `narrative_source` says so on every response.\n\n"
            "Needs a committed baseline to compare against. If none exists the call "
            "fails with an explanation rather than returning an empty update, because "
            "'nothing to compare' and 'nothing changed' mean opposite things.\n\n"
            "`current` and `baseline` accept the same references as plan.get (version, "
            "hash prefix, 'latest', 'baseline'); both default sensibly. `period` labels "
            "the reporting window. This drafts only — it sends no email and posts to no "
            "channel, and changes nothing."
        ),
    )
    @legible_errors
    def status_draft(
        current: str | None = None,
        baseline: str | None = None,
        start: str | None = None,
        period: str | None = None,
    ) -> StatusDraft:
        try:
            baseline_plan = resolve_plan_ref(baseline or "baseline")
        except PlanNotFound as exc:
            # Deliberately does not quote the underlying message: the resolver's
            # advice ("omit the reference to use the default plan") is right for
            # plan.get and actively misleading here, where the only fix is to
            # commit a baseline.
            raise PlanNotFound(
                "No baseline has been committed, so there is nothing to compare this "
                "period against. That is not the same as 'nothing changed' — nothing "
                "is known. Commit one with `plan baseline`, or pass an explicit "
                "`baseline` reference; plan.list shows what exists."
            ) from exc

        current_plan = resolve_plan_ref(current)
        start_date = start_date_or_default(start)

        comparison = compare_versions(
            baseline_plan.plan, current_plan.plan, start_date=start_date
        )
        facts = assemble_status(
            comparison,
            baseline_raid=baseline_plan.plan.raid,
            current_raid=current_plan.plan.raid,
            period_label=period or "This week",
            baseline_version=baseline_plan.version,
        )
        narrative = fallback_narrative(facts)

        lists = {
            "slipped": _tasks(facts.slipped),
            "newly_critical": _tasks(facts.newly_critical),
            "no_longer_critical": _tasks(facts.no_longer_critical),
            "milestone_drift": _milestones(facts.milestone_drift),
            "breaches": _breaches(facts.breaches),
            "raid_added": _raid(facts.raid_added),
            "raid_removed": _raid(facts.raid_removed),
        }
        truncated = [key for key, value in lists.items() if len(value) > MAX_ITEMS_PER_LIST]
        trimmed = {key: value[:MAX_ITEMS_PER_LIST] for key, value in lists.items()}

        return StatusDraft(
            current=PlanRef.of(current_plan),
            baseline=PlanRef.of(baseline_plan),
            baseline_version=baseline_plan.version,
            baseline_note=baseline_plan.message,
            start_date=start_date,
            facts=StatusFactsOut(
                period_label=facts.period_label,
                health=facts.health.value,
                health_reasons=facts.health_reasons,
                launch_before=facts.launch_before,
                launch_after=facts.launch_after,
                launch_shift_working_days=facts.launch_shift_days,
                structural_change_count=facts.structural_change_count,
                truncated_lists=truncated,
                **trimmed,
            ),
            exec_summary=narrative.exec_summary,
            points=narrative.points,
            markdown=render_markdown(facts, narrative),
            narrative_source="deterministic",
            computed_at=datetime.now(UTC),
        )
