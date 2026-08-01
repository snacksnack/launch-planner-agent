"""Slippage simulator — deterministic what-if analysis over the CPM schedule.

Answers "what happens if data migration slips 5 days?" without any model in the
loop: apply a hypothetical `Scenario` to a *copy* of the plan, re-run the same CPM
engine, and diff the two schedules. Because it reuses `schedule_plan`, a slip's
effect is exactly the textbook float behaviour — a critical task's slip moves the
launch by that amount; a slip smaller than a task's total float is absorbed with
zero launch impact.

A scenario is a list of typed changes (delay a task, override an estimate, add or
remove a dependency). Applying it never raises on bad input: unknown ids, self
loops, duplicate or cycle-creating edges, and invalid estimates are collected as
`warnings` and skipped, so the recompute always produces a schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Literal

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from planner_core.models import Dependency, DependencyType, Plan, ThreePointEstimate
from planner_core.provenance import Confidence, Provenance
from planner_core.scheduling import Schedule, schedule_plan

_DEFAULT_WEEKEND = frozenset({5, 6})

# Simulated edges are throwaway what-ifs, not plan-of-record entities, but a
# Dependency still requires provenance — stamp a fixed, self-describing block.
_SIM_PROVENANCE = Provenance(
    reasoning="What-if scenario edge (not part of the plan of record).",
    source_quote="(what-if scenario — no source document)",
    source_section=None,
    confidence=Confidence.MEDIUM,
    agent="simulation",
    model="deterministic",
    timestamp=datetime(1970, 1, 1, tzinfo=UTC),
)


# --- scenario model --------------------------------------------------------


class DelayTask(BaseModel):
    """A task slips: it takes `days` more working days than planned."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["delay_task"] = "delay_task"
    task_id: str
    days: float = Field(..., gt=0, description="Working days of slip (positive).")


class SetEstimate(BaseModel):
    """Override any of a task's three-point estimate values (working days)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["set_estimate"] = "set_estimate"
    task_id: str
    optimistic: float | None = None
    likely: float | None = None
    pessimistic: float | None = None


class AddDependency(BaseModel):
    """Add a hypothetical precedence edge."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["add_dependency"] = "add_dependency"
    predecessor_id: str
    successor_id: str
    dependency_type: DependencyType = DependencyType.FINISH_TO_START
    lag: float = 0.0


class RemoveDependency(BaseModel):
    """Remove an existing precedence edge (matched by its endpoints)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["remove_dependency"] = "remove_dependency"
    predecessor_id: str
    successor_id: str


ScenarioChange = Annotated[
    DelayTask | SetEstimate | AddDependency | RemoveDependency,
    Field(discriminator="kind"),
]


class Scenario(BaseModel):
    """A named bundle of hypothetical changes to apply to a plan."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    changes: list[ScenarioChange] = []


class SavedScenario(BaseModel):
    """A `Scenario` persisted under a name, scoped to the plan it was built against.

    A saved scenario is a reviewer's scratchpad entry, not a plan-of-record entity:
    it lives *beside* the store (a mutable catalog), keyed by `plan_hash` — the
    content hash of the target plan. Because the hash pins the exact plan, reloading
    a saved scenario reproduces the identical `ScheduleDelta`. Carries light
    provenance (who saved it, when) in keeping with the system's audit ethos.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    plan_hash: str
    scenario: Scenario
    created_by: str | None = None
    created_at: datetime
    note: str | None = None


# --- applying a scenario ---------------------------------------------------


def _coalesce(new: float | None, old: float) -> float:
    return old if new is None else new


def _would_create_cycle(deps: list[Dependency], edge: tuple[str, str], nodes: set[str]) -> bool:
    graph = nx.DiGraph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from((d.predecessor_id, d.successor_id) for d in deps)
    graph.add_edge(*edge)
    return not nx.is_directed_acyclic_graph(graph)


def apply_scenario(plan: Plan, scenario: Scenario) -> tuple[Plan, list[str]]:
    """Return a copy of `plan` with the scenario applied, plus any warnings.

    Invalid changes (unknown ids, self loops, duplicate or cycle-creating edges,
    estimates that violate optimistic <= likely <= pessimistic) are skipped and
    reported as warnings rather than raising, so the recompute never fails.
    """
    working = plan.model_copy(deep=True)
    task_by_id = {t.id: t for t in working.tasks}
    node_ids = {t.id for t in working.tasks} | {m.id for m in working.milestones}
    warnings: list[str] = []
    added = 0

    for change in scenario.changes:
        if isinstance(change, DelayTask):
            task = task_by_id.get(change.task_id)
            if task is None:
                warnings.append(f"delay_task: unknown task {change.task_id!r} — skipped")
                continue
            est = task.estimate
            new_likely = est.likely + change.days
            task.estimate = ThreePointEstimate(
                optimistic=est.optimistic,
                likely=new_likely,
                pessimistic=max(est.pessimistic, new_likely),
            )

        elif isinstance(change, SetEstimate):
            task = task_by_id.get(change.task_id)
            if task is None:
                warnings.append(f"set_estimate: unknown task {change.task_id!r} — skipped")
                continue
            est = task.estimate
            try:
                task.estimate = ThreePointEstimate(
                    optimistic=_coalesce(change.optimistic, est.optimistic),
                    likely=_coalesce(change.likely, est.likely),
                    pessimistic=_coalesce(change.pessimistic, est.pessimistic),
                )
            except ValidationError:
                warnings.append(
                    f"set_estimate: {change.task_id!r} values violate "
                    "optimistic <= likely <= pessimistic — skipped"
                )

        elif isinstance(change, RemoveDependency):
            pair = (change.predecessor_id, change.successor_id)
            kept = [d for d in working.dependencies if (d.predecessor_id, d.successor_id) != pair]
            if len(kept) == len(working.dependencies):
                warnings.append(
                    f"remove_dependency: no edge {pair[0]} -> {pair[1]} — skipped"
                )
            working.dependencies = kept

        elif isinstance(change, AddDependency):
            pred, succ = change.predecessor_id, change.successor_id
            if pred not in node_ids or succ not in node_ids:
                missing = pred if pred not in node_ids else succ
                warnings.append(f"add_dependency: unknown id {missing!r} — skipped")
                continue
            if pred == succ:
                warnings.append("add_dependency: a node cannot depend on itself — skipped")
                continue
            if any(
                (d.predecessor_id, d.successor_id) == (pred, succ) for d in working.dependencies
            ):
                warnings.append(f"add_dependency: {pred} -> {succ} already exists — skipped")
                continue
            if _would_create_cycle(working.dependencies, (pred, succ), node_ids):
                warnings.append(f"add_dependency: {pred} -> {succ} would create a cycle — skipped")
                continue
            added += 1
            working.dependencies.append(
                Dependency(
                    id=f"sim-dep-{added}",
                    predecessor_id=pred,
                    successor_id=succ,
                    type=change.dependency_type,
                    lag=change.lag,
                    provenance=_SIM_PROVENANCE,
                )
            )

    return working, warnings


# --- schedule diff ---------------------------------------------------------


class TaskShift(BaseModel):
    """A task whose schedule moved between baseline and simulated."""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    task_name: str
    start_before: date
    start_after: date
    finish_before: date
    finish_after: date
    finish_shift_days: int  # simulated finish - baseline finish, in working days
    total_float_before: float
    total_float_after: float
    became_critical: bool
    left_critical: bool


class MilestoneShift(BaseModel):
    """A milestone whose projected date or slack moved."""

    model_config = ConfigDict(extra="forbid")
    milestone_id: str
    milestone_name: str
    projected_before: date | None
    projected_after: date | None
    slack_before: int | None
    slack_after: int | None


class DeadlineFlip(BaseModel):
    """A hard-date check whose met/slack status changed."""

    model_config = ConfigDict(extra="forbid")
    constraint_id: str
    task_id: str
    met_before: bool
    met_after: bool
    slack_before: int
    slack_after: int


class NamedId(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str


class ScheduleDelta(BaseModel):
    """The structured difference between two CPM schedules."""

    model_config = ConfigDict(extra="forbid")

    finish_before: date | None
    finish_after: date | None
    finish_shift_days: int  # +N later, -N earlier, 0 unchanged
    task_shifts: list[TaskShift] = []
    milestone_shifts: list[MilestoneShift] = []
    critical_joined: list[NamedId] = []
    critical_left: list[NamedId] = []
    deadline_flips: list[DeadlineFlip] = []
    headline: str = ""
    notes: list[str] = []

    @property
    def has_launch_impact(self) -> bool:
        return self.finish_shift_days != 0

    def render(self) -> str:
        lines = [self.headline, *(f"  - {n}" for n in self.notes)]
        for s in self.task_shifts:
            lines.append(
                f"  ~ {s.task_id}: {s.finish_before} -> {s.finish_after} "
                f"({s.finish_shift_days:+d}d), float {s.total_float_before:g} -> "
                f"{s.total_float_after:g}"
            )
        return "\n".join(lines)


def _round(x: float) -> int:
    return int(round(x))


def diff_schedules(baseline: Schedule, simulated: Schedule, plan: Plan) -> ScheduleDelta:
    """Diff two schedules of the same plan shape into a structured delta."""
    name_by_id = {t.id: t.name for t in plan.tasks}
    name_by_id.update({m.id: m.name for m in plan.milestones})

    finish_shift = _round(simulated.project_duration - baseline.project_duration)

    task_shifts: list[TaskShift] = []
    base_critical: set[str] = set()
    sim_critical: set[str] = set()
    for tid, base in baseline.tasks.items():
        sim = simulated.tasks.get(tid)
        if sim is None:
            continue
        if base.is_critical:
            base_critical.add(tid)
        if sim.is_critical:
            sim_critical.add(tid)
        moved = (
            base.early_start_date != sim.early_start_date
            or base.early_finish_date != sim.early_finish_date
            or base.total_float != sim.total_float
            or base.is_critical != sim.is_critical
        )
        if moved:
            task_shifts.append(
                TaskShift(
                    task_id=tid,
                    task_name=name_by_id.get(tid, tid),
                    start_before=base.early_start_date,
                    start_after=sim.early_start_date,
                    finish_before=base.early_finish_date,
                    finish_after=sim.early_finish_date,
                    finish_shift_days=_round(sim.early_finish - base.early_finish),
                    total_float_before=base.total_float,
                    total_float_after=sim.total_float,
                    became_critical=sim.is_critical and not base.is_critical,
                    left_critical=base.is_critical and not sim.is_critical,
                )
            )

    milestone_shifts: list[MilestoneShift] = []
    base_ms = {m.milestone_id: m for m in baseline.milestones}
    for sim_m in simulated.milestones:
        base_m = base_ms.get(sim_m.milestone_id)
        if base_m is None:
            continue
        if base_m.projected_date != sim_m.projected_date or (
            base_m.slack_working_days != sim_m.slack_working_days
        ):
            milestone_shifts.append(
                MilestoneShift(
                    milestone_id=sim_m.milestone_id,
                    milestone_name=name_by_id.get(sim_m.milestone_id, sim_m.milestone_id),
                    projected_before=base_m.projected_date,
                    projected_after=sim_m.projected_date,
                    slack_before=base_m.slack_working_days,
                    slack_after=sim_m.slack_working_days,
                )
            )

    deadline_flips: list[DeadlineFlip] = []
    base_dl = {(c.constraint_id, c.task_id): c for c in baseline.deadline_checks}
    for sim_c in simulated.deadline_checks:
        base_c = base_dl.get((sim_c.constraint_id, sim_c.task_id))
        if base_c is None:
            continue
        if base_c.met != sim_c.met or base_c.slack_working_days != sim_c.slack_working_days:
            deadline_flips.append(
                DeadlineFlip(
                    constraint_id=sim_c.constraint_id,
                    task_id=sim_c.task_id,
                    met_before=base_c.met,
                    met_after=sim_c.met,
                    slack_before=base_c.slack_working_days,
                    slack_after=sim_c.slack_working_days,
                )
            )

    def _named(ids: set[str]) -> list[NamedId]:
        return [NamedId(id=i, name=name_by_id.get(i, i)) for i in sorted(ids)]

    joined = _named(sim_critical - base_critical)
    left = _named(base_critical - sim_critical)

    headline, notes = _narrate(
        finish_shift, baseline, simulated, joined, left, deadline_flips
    )

    return ScheduleDelta(
        finish_before=baseline.project_finish_date,
        finish_after=simulated.project_finish_date,
        finish_shift_days=finish_shift,
        task_shifts=task_shifts,
        milestone_shifts=milestone_shifts,
        critical_joined=joined,
        critical_left=left,
        deadline_flips=deadline_flips,
        headline=headline,
        notes=notes,
    )


def _narrate(
    finish_shift: int,
    baseline: Schedule,
    simulated: Schedule,
    joined: list[NamedId],
    left: list[NamedId],
    deadline_flips: list[DeadlineFlip],
) -> tuple[str, list[str]]:
    after = simulated.project_finish_date
    before = baseline.project_finish_date
    if finish_shift == 0:
        headline = (
            f"No impact on the projected launch date ({after}) — "
            "the change is absorbed by available float."
        )
    elif finish_shift > 0:
        headline = f"Launch slips {finish_shift} working day(s): {before} → {after}."
    else:
        headline = f"Launch pulls in {abs(finish_shift)} working day(s): {before} → {after}."

    notes: list[str] = []
    for n in joined:
        notes.append(f"{n.name} becomes critical.")
    for n in left:
        notes.append(f"{n.name} is no longer critical.")
    for f in deadline_flips:
        if f.met_before and not f.met_after:
            notes.append(f"Deadline {f.constraint_id} now MISSED ({f.slack_after:+d}d).")
        elif not f.met_before and f.met_after:
            notes.append(f"Deadline {f.constraint_id} now met ({f.slack_after:+d} working days).")
    return headline, notes


# --- top-level entry -------------------------------------------------------


@dataclass(frozen=True)
class SimulationResult:
    """Baseline + simulated schedules, the applied plan, the delta, and warnings."""

    scenario: Scenario
    baseline: Schedule
    simulated: Schedule
    simulated_plan: Plan
    delta: ScheduleDelta
    warnings: list[str]


def simulate(
    plan: Plan,
    scenario: Scenario,
    *,
    start_date: date,
    weekend: frozenset[int] = _DEFAULT_WEEKEND,
    blackouts: tuple[tuple[date, date], ...] = (),
) -> SimulationResult:
    """Schedule the plan, apply the scenario to a copy, re-schedule, and diff."""
    baseline = schedule_plan(plan, start_date=start_date, weekend=weekend, blackouts=blackouts)
    simulated_plan, warnings = apply_scenario(plan, scenario)
    simulated = schedule_plan(
        simulated_plan, start_date=start_date, weekend=weekend, blackouts=blackouts
    )
    delta = diff_schedules(baseline, simulated, plan)
    return SimulationResult(
        scenario=scenario,
        baseline=baseline,
        simulated=simulated,
        simulated_plan=simulated_plan,
        delta=delta,
        warnings=warnings,
    )
