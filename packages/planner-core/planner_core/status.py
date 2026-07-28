"""Weekly status assembly — deterministic facts + a rule-based health signal.

The status update is the exec-facing bookend of the audit story: *what changed
since last week, and are we still OK?* The hard rule here, mirroring the rest of
the system, is that **the LLM never decides status** — it only phrases it. This
module assembles the week's facts from the RC1-192 changed-since diff and derives
a green/yellow/red **health indicator by explicit rule** (a missed deadline or a
big launch slip is red; any slip, a newly-critical task, or a new risk is yellow).
Every fact is traceable to a diff entry, so the narrative the agent writes on top
can be checked against ground truth.

`assemble_status` returns `StatusFacts`; `render_markdown` / `render_html` turn a
`StatusFacts` + a `StatusNarrative` (LLM prose, or the deterministic fallback)
into the email. Pure and testable — no LLM, no I/O.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from planner_core.baseline import BaselineComparison
from planner_core.raid import RaidItem

# A launch slip at or beyond this many working days is red on its own.
_RED_SLIP_DAYS = 10


class Health(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class NamedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    shift_days: int = 0


class MilestoneDrift(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    projected_before: date | None
    projected_after: date | None
    slack_shift_days: int | None


class Breach(BaseModel):
    model_config = ConfigDict(extra="forbid")
    constraint_id: str
    task_id: str
    slack_days: int


class RaidChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: str
    title: str
    severity: int | None = None


class StatusFacts(BaseModel):
    """The deterministic, diff-traceable facts of a reporting period."""

    model_config = ConfigDict(extra="forbid")

    period_label: str
    baseline_version: int | None = None
    health: Health
    health_reasons: list[str] = []

    launch_before: date | None = None
    launch_after: date | None = None
    launch_shift_days: int = 0

    slipped: list[NamedChange] = []
    newly_critical: list[NamedChange] = []
    no_longer_critical: list[NamedChange] = []
    milestone_drift: list[MilestoneDrift] = []
    breaches: list[Breach] = []
    raid_added: list[RaidChange] = []
    raid_removed: list[RaidChange] = []
    structural_change_count: int = 0

    @property
    def is_on_track(self) -> bool:
        return self.health is Health.GREEN


class StatusNarrative(BaseModel):
    """The prose layer: an exec summary + 'what changed' points. LLM- or rule-written."""

    model_config = ConfigDict(extra="forbid")

    exec_summary: str
    points: list[str] = []


def _raid_change(item: RaidItem) -> RaidChange:
    return RaidChange(id=item.id, type=item.type.value, title=item.title, severity=item.severity)


def _health(
    launch_shift: int, newly_critical: list, breaches: list, raid_added: list
) -> tuple[Health, list[str]]:
    """Deterministic health rule (never the LLM). Returns (health, reasons)."""
    reasons: list[str] = []
    health = Health.GREEN

    if breaches:
        health = Health.RED
        reasons.append(f"{len(breaches)} deadline(s) now missed")
    if launch_shift >= _RED_SLIP_DAYS:
        health = Health.RED
        reasons.append(f"launch slipped {launch_shift} working days (>= {_RED_SLIP_DAYS})")

    if health is not Health.RED:
        if launch_shift > 0:
            health = Health.YELLOW
            reasons.append(f"launch slipped {launch_shift} working day(s)")
        if newly_critical:
            health = Health.YELLOW
            reasons.append(f"{len(newly_critical)} task(s) newly on the critical path")
        if raid_added:
            health = Health.YELLOW
            reasons.append(f"{len(raid_added)} new RAID item(s)")

    if health is Health.GREEN:
        if launch_shift < 0:
            reasons.append(f"launch pulled in {abs(launch_shift)} working day(s)")
        else:
            reasons.append("no launch slip, no new critical-path tasks, no missed deadlines")

    return health, reasons


def assemble_status(
    comparison: BaselineComparison,
    *,
    baseline_raid: list[RaidItem],
    current_raid: list[RaidItem],
    period_label: str,
    baseline_version: int | None = None,
) -> StatusFacts:
    """Assemble the period's facts from the changed-since diff + RAID delta.

    Every field derives from `comparison` (the RC1-192 baseline diff) or the RAID
    lists, so the narrative written on top is checkable against these facts.
    """
    delta = comparison.schedule_delta

    slipped = [
        NamedChange(id=s.task_id, name=s.task_name, shift_days=s.finish_shift_days)
        for s in delta.task_shifts
        if s.finish_shift_days > 0
    ]
    newly_critical = [NamedChange(id=n.id, name=n.name) for n in delta.critical_joined]
    no_longer_critical = [NamedChange(id=n.id, name=n.name) for n in delta.critical_left]
    milestone_drift = [
        MilestoneDrift(
            id=m.milestone_id,
            name=m.milestone_name,
            projected_before=m.projected_before,
            projected_after=m.projected_after,
            slack_shift_days=(
                (m.slack_before - m.slack_after)
                if m.slack_before is not None and m.slack_after is not None
                else None
            ),
        )
        for m in delta.milestone_shifts
    ]
    breaches = [
        Breach(constraint_id=f.constraint_id, task_id=f.task_id, slack_days=f.slack_after)
        for f in delta.deadline_flips
        if f.met_before and not f.met_after
    ]

    base_ids = {r.id for r in baseline_raid}
    curr_ids = {r.id for r in current_raid}
    raid_added = [_raid_change(r) for r in current_raid if r.id not in base_ids]
    raid_removed = [_raid_change(r) for r in baseline_raid if r.id not in curr_ids]

    health, reasons = _health(delta.finish_shift_days, newly_critical, breaches, raid_added)

    return StatusFacts(
        period_label=period_label,
        baseline_version=baseline_version,
        health=health,
        health_reasons=reasons,
        launch_before=delta.finish_before,
        launch_after=delta.finish_after,
        launch_shift_days=delta.finish_shift_days,
        slipped=slipped,
        newly_critical=newly_critical,
        no_longer_critical=no_longer_critical,
        milestone_drift=milestone_drift,
        breaches=breaches,
        raid_added=raid_added,
        raid_removed=raid_removed,
        structural_change_count=len(comparison.plan_diff.entities),
    )


# --- deterministic fallback narrative (used when no LLM is available) --------


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def fallback_narrative(facts: StatusFacts) -> StatusNarrative:
    """A serviceable rule-written narrative so the report works without an LLM."""
    verb = {"green": "on track", "yellow": "at some risk", "red": "off track"}[facts.health.value]
    if facts.launch_shift_days == 0:
        launch = f"the projected launch holds at {facts.launch_after}"
    elif facts.launch_shift_days > 0:
        launch = (
            f"the projected launch slipped {facts.launch_shift_days} working day(s) to "
            f"{facts.launch_after}"
        )
    else:
        launch = (
            f"the projected launch pulled in {abs(facts.launch_shift_days)} working day(s) to "
            f"{facts.launch_after}"
        )
    summary = f"The plan is {verb}: {launch}."

    points: list[str] = []
    for b in facts.breaches:
        points.append(f"Deadline {b.constraint_id} is now missed ({_signed(b.slack_days)}d).")
    for s in facts.slipped:
        points.append(f"{s.name} slipped {s.shift_days} working day(s).")
    for n in facts.newly_critical:
        points.append(f"{n.name} is newly on the critical path.")
    for m in facts.milestone_drift:
        if m.projected_before != m.projected_after:
            points.append(f"Milestone {m.name} moved {m.projected_before} → {m.projected_after}.")
    for r in facts.raid_added:
        sev = f" (severity {r.severity})" if r.severity is not None else ""
        points.append(f"New {r.type}: {r.title}{sev}.")
    if not points:
        points.append("No material changes since the baseline.")
    return StatusNarrative(exec_summary=summary, points=points)


# --- renderers --------------------------------------------------------------

_HEALTH_LABEL = {"green": "On track", "yellow": "At risk", "red": "Off track"}
_HEALTH_COLOR = {"green": "#0f7b3f", "yellow": "#9a6700", "red": "#c0392b"}


def render_markdown(facts: StatusFacts, narrative: StatusNarrative) -> str:
    lines = [
        f"# Status update — {facts.period_label}",
        "",
        f"**Health: {_HEALTH_LABEL[facts.health.value]}** "
        f"({'; '.join(facts.health_reasons)})",
        "",
        narrative.exec_summary,
        "",
        "## What changed since last week",
    ]
    lines.extend(f"- {p}" for p in narrative.points)
    return "\n".join(lines)


def render_html(facts: StatusFacts, narrative: StatusNarrative) -> str:
    color = _HEALTH_COLOR[facts.health.value]
    points = "".join(f"<li>{p}</li>" for p in narrative.points)
    return (
        '<div style="font-family:system-ui,-apple-system,sans-serif;max-width:640px">'
        f"<h1 style=\"font-size:18px\">Status update — {facts.period_label}</h1>"
        f'<p><span style="display:inline-block;padding:2px 10px;border-radius:999px;'
        f'background:{color};color:#fff;font-weight:600">'
        f"{_HEALTH_LABEL[facts.health.value]}</span> "
        f'<span style="color:#5b657a">{"; ".join(facts.health_reasons)}</span></p>'
        f"<p>{narrative.exec_summary}</p>"
        '<h2 style="font-size:14px">What changed since last week</h2>'
        f"<ul>{points}</ul>"
        "</div>"
    )
