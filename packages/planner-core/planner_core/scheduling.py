"""Critical Path Method scheduling — the deterministic heart, zero LLM.

Classic CPM over the validated dependency DAG: a forward pass (early start/finish)
and backward pass (late start/finish) using the *likely* estimate as each task's
duration, then total float, free float, and the critical path (the zero-float
chain(s)). All four PDM relationship types (finish-to-start, start-to-start,
finish-to-finish, start-to-finish) and lags are supported.

The CPM math (`compute_cpm`) is pure and calendar-agnostic — it works in
*working-day offsets* from the project start, which is exactly what makes it
testable against hand-computed textbook examples. A separate `WorkingCalendar`
maps those offsets onto real dates, skipping weekends and blackout/freeze windows
(a freeze is simply a stretch of non-working days, so work routes around it
without changing any float). `schedule_plan` ties them together and adds
hard-date deadline checks and milestone projections.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import networkx as nx

from planner_core.models import ConstraintType, DependencyType, Plan

_EPS = 1e-9


# --- pure numeric CPM ------------------------------------------------------


@dataclass(frozen=True)
class NodeMetrics:
    early_start: float
    early_finish: float
    late_start: float
    late_finish: float
    total_float: float
    free_float: float
    is_critical: bool


@dataclass(frozen=True)
class CPMResult:
    nodes: dict[str, NodeMetrics]
    project_duration: float
    critical_edges: frozenset[tuple[str, str]]


EdgeSpec = tuple[str, str, DependencyType, float]


def _forward_required(
    pred_es: float, pred_ef: float, succ_dur: float, dtype: DependencyType, lag: float
) -> float:
    """Earliest start the edge forces on the successor."""
    if dtype is DependencyType.FINISH_TO_START:
        return pred_ef + lag
    if dtype is DependencyType.START_TO_START:
        return pred_es + lag
    if dtype is DependencyType.FINISH_TO_FINISH:
        return pred_ef + lag - succ_dur
    # START_TO_FINISH
    return pred_es + lag - succ_dur


def _backward_bound(
    succ_ls: float, succ_lf: float, pred_dur: float, dtype: DependencyType, lag: float
) -> float:
    """Latest finish the edge allows the predecessor."""
    if dtype is DependencyType.FINISH_TO_START:
        return succ_ls - lag
    if dtype is DependencyType.FINISH_TO_FINISH:
        return succ_lf - lag
    if dtype is DependencyType.START_TO_START:
        return succ_ls - lag + pred_dur
    # START_TO_FINISH
    return succ_lf - lag + pred_dur


def compute_cpm(durations: dict[str, float], edges: list[EdgeSpec]) -> CPMResult:
    """Run CPM over a DAG of durations and precedence edges (working-day units).

    Raises ValueError on a cyclic graph — cycles must be resolved upstream
    (the Dependency Agent's `resolve_cycles`) before scheduling.
    """
    graph = nx.DiGraph()
    graph.add_nodes_from(durations)
    for pred, succ, dtype, lag in edges:
        graph.add_edge(pred, succ, dtype=dtype, lag=lag)
    if not nx.is_directed_acyclic_graph(graph):
        raise ValueError("cannot schedule a cyclic dependency graph")

    order = list(nx.topological_sort(graph))

    # Forward pass: earliest start/finish.
    es = dict.fromkeys(durations, 0.0)
    ef: dict[str, float] = {}
    for n in order:
        ef[n] = es[n] + durations[n]
        for m in graph.successors(n):
            data = graph[n][m]
            required = _forward_required(es[n], ef[n], durations[m], data["dtype"], data["lag"])
            if required > es[m]:
                es[m] = required
    project_duration = max(ef.values(), default=0.0)

    # Backward pass: latest start/finish.
    lf = dict.fromkeys(durations, project_duration)
    ls: dict[str, float] = {}
    for n in reversed(order):
        for m in graph.successors(n):
            data = graph[n][m]
            bound = _backward_bound(ls[m], lf[m], durations[n], data["dtype"], data["lag"])
            if bound < lf[n]:
                lf[n] = bound
        ls[n] = lf[n] - durations[n]

    total_float = {n: ls[n] - es[n] for n in durations}
    critical = {n: abs(total_float[n]) < _EPS for n in durations}

    # Free float: min slack on out-edges (how far a task can slip without
    # delaying any successor's early start). No successors -> bounded by total.
    free_float: dict[str, float] = {}
    critical_edges: set[tuple[str, str]] = set()
    for n in durations:
        successors = list(graph.successors(n))
        if not successors:
            free_float[n] = total_float[n]
            continue
        slacks = []
        for m in successors:
            data = graph[n][m]
            required = _forward_required(es[n], ef[n], durations[m], data["dtype"], data["lag"])
            edge_slack = es[m] - required
            slacks.append(edge_slack)
            if abs(edge_slack) < _EPS and critical[n] and critical[m]:
                critical_edges.add((n, m))
        free_float[n] = max(0.0, min(slacks))

    nodes = {
        n: NodeMetrics(
            early_start=es[n],
            early_finish=ef[n],
            late_start=ls[n],
            late_finish=lf[n],
            total_float=total_float[n],
            free_float=free_float[n],
            is_critical=critical[n],
        )
        for n in durations
    }
    return CPMResult(
        nodes=nodes,
        project_duration=project_duration,
        critical_edges=frozenset(critical_edges),
    )


def critical_paths(result: CPMResult) -> list[list[str]]:
    """Enumerate the critical chain(s) as ordered task-id sequences."""
    critical_nodes = {n for n, m in result.nodes.items() if m.is_critical}
    graph = nx.DiGraph()
    graph.add_nodes_from(critical_nodes)
    graph.add_edges_from(result.critical_edges)

    sources = [n for n in critical_nodes if graph.in_degree(n) == 0]
    paths: list[list[str]] = []
    for source in sorted(sources):
        sinks = [n for n in critical_nodes if graph.out_degree(n) == 0]
        if graph.out_degree(source) == 0:
            paths.append([source])
            continue
        for sink in sorted(sinks):
            paths.extend(list(nx.all_simple_paths(graph, source, sink)))
    return paths


# --- working-day calendar --------------------------------------------------

_DEFAULT_WEEKEND = frozenset({5, 6})  # Saturday, Sunday (Monday == 0)


@dataclass(frozen=True)
class WorkingCalendar:
    """Maps working-day offsets to calendar dates, skipping weekends and blackouts.

    `blackouts` are inclusive `(start, end)` date ranges during which no work
    happens — e.g. a Q4 change freeze. They are treated as non-working days for
    every task, so the schedule naturally flows around them.
    """

    start_date: date
    weekend: frozenset[int] = _DEFAULT_WEEKEND
    blackouts: tuple[tuple[date, date], ...] = ()

    def is_working_day(self, day: date) -> bool:
        if day.weekday() in self.weekend:
            return False
        return not any(start <= day <= end for start, end in self.blackouts)

    def nth_working_day(self, index: int) -> date:
        """The date of the 0-based `index`-th working day on/after `start_date`."""
        day = self.start_date
        while not self.is_working_day(day):
            day += timedelta(days=1)
        count = 0
        while count < index:
            day += timedelta(days=1)
            if self.is_working_day(day):
                count += 1
        return day

    def signed_working_days(self, frm: date, to: date) -> int:
        """Signed count of working days from `frm` to `to` (negative if `to` is earlier)."""
        if frm == to:
            return 0
        sign = 1 if to > frm else -1
        lo, hi = sorted((frm, to))
        count = 0
        day = lo + timedelta(days=1)
        while day <= hi:
            if self.is_working_day(day):
                count += 1
            day += timedelta(days=1)
        return sign * count

    def start_date_of(self, offset: float) -> date:
        return self.nth_working_day(math.floor(offset + _EPS))

    def finish_date_of(self, early_start: float, early_finish: float) -> date:
        """Calendar date of the last working day a task is active (its start day
        if it has zero duration, e.g. a milestone)."""
        if early_finish - early_start < 1:
            return self.start_date_of(early_start)
        return self.nth_working_day(math.ceil(early_finish - _EPS) - 1)


# --- plan-level scheduling -------------------------------------------------


@dataclass(frozen=True)
class TaskSchedule:
    task_id: str
    duration: float
    early_start: float
    early_finish: float
    late_start: float
    late_finish: float
    total_float: float
    free_float: float
    is_critical: bool
    early_start_date: date
    early_finish_date: date


@dataclass(frozen=True)
class MilestoneSchedule:
    milestone_id: str
    target_date: date | None
    projected_date: date | None
    slack_working_days: int | None  # target - projected; negative = late
    scheduled: bool


@dataclass(frozen=True)
class DeadlineCheck:
    constraint_id: str
    task_id: str
    deadline: date
    projected_finish_date: date
    slack_working_days: int  # negative = the plan misses the date
    met: bool


@dataclass(frozen=True)
class Schedule:
    start_date: date
    project_duration: float
    project_finish_date: date | None
    tasks: dict[str, TaskSchedule]
    milestones: list[MilestoneSchedule]
    critical_path_ids: list[str]
    critical_chains: list[list[str]]
    deadline_checks: list[DeadlineCheck]

    @property
    def meets_all_deadlines(self) -> bool:
        return all(check.met for check in self.deadline_checks)

    def render(self) -> str:
        finish = self.project_finish_date.isoformat() if self.project_finish_date else "n/a"
        lines = [
            f"Projected finish: {finish}  "
            f"({self.project_duration:g} working days from {self.start_date.isoformat()})",
            f"Critical path ({len(self.critical_path_ids)} tasks): "
            + (" -> ".join(self.critical_path_ids) or "none"),
        ]
        if self.deadline_checks:
            lines.append("Hard-date checks:")
            for check in self.deadline_checks:
                verdict = "OK" if check.met else "MISS"
                lines.append(
                    f"  [{verdict}] {check.task_id} vs {check.constraint_id} "
                    f"({check.deadline.isoformat()}): "
                    f"{check.slack_working_days:+d} working days of slack"
                )
        scheduled_ms = [m for m in self.milestones if m.scheduled]
        if scheduled_ms:
            lines.append("Milestones:")
            for m in scheduled_ms:
                target = m.target_date.isoformat() if m.target_date else "no target"
                projected = m.projected_date.isoformat() if m.projected_date else "n/a"
                slack = f"{m.slack_working_days:+d}d" if m.slack_working_days is not None else "n/a"
                lines.append(
                    f"  {m.milestone_id}: projected {projected}, target {target} ({slack})"
                )
        return "\n".join(lines)


def schedule_plan(
    plan: Plan,
    *,
    start_date: date,
    weekend: frozenset[int] = _DEFAULT_WEEKEND,
    blackouts: tuple[tuple[date, date], ...] = (),
) -> Schedule:
    """Schedule a validated plan: CPM over tasks, mapped onto a working calendar.

    Milestones are included as zero-duration nodes; a milestone is only reported
    as *scheduled* when a dependency edge actually reaches it (on today's plans
    milestones are typically unlinked, so they carry only their target date).
    """
    calendar = WorkingCalendar(start_date=start_date, weekend=weekend, blackouts=blackouts)

    durations: dict[str, float] = {t.id: t.estimate.likely for t in plan.tasks}
    for milestone in plan.milestones:
        durations[milestone.id] = 0.0

    edges: list[EdgeSpec] = [
        (d.predecessor_id, d.successor_id, d.type, d.lag) for d in plan.dependencies
    ]
    result = compute_cpm(durations, edges)

    task_schedules = {
        t.id: TaskSchedule(
            task_id=t.id,
            duration=durations[t.id],
            early_start=result.nodes[t.id].early_start,
            early_finish=result.nodes[t.id].early_finish,
            late_start=result.nodes[t.id].late_start,
            late_finish=result.nodes[t.id].late_finish,
            total_float=result.nodes[t.id].total_float,
            free_float=result.nodes[t.id].free_float,
            is_critical=result.nodes[t.id].is_critical,
            early_start_date=calendar.start_date_of(result.nodes[t.id].early_start),
            early_finish_date=calendar.finish_date_of(
                result.nodes[t.id].early_start, result.nodes[t.id].early_finish
            ),
        )
        for t in plan.tasks
    }

    # Which nodes have an incoming edge (used to tell linked milestones apart).
    linked = {d.successor_id for d in plan.dependencies}

    milestone_schedules: list[MilestoneSchedule] = []
    for milestone in plan.milestones:
        if milestone.id in linked:
            # A milestone is "reached" on the day its gating work finishes — the
            # same finish-day convention used for task early_finish_date.
            ef_m = result.nodes[milestone.id].early_finish
            projected = (
                calendar.nth_working_day(math.ceil(ef_m - _EPS) - 1)
                if ef_m >= 1
                else calendar.start_date_of(ef_m)
            )
            slack = (
                calendar.signed_working_days(projected, milestone.target_date)
                if milestone.target_date
                else None
            )
            milestone_schedules.append(
                MilestoneSchedule(milestone.id, milestone.target_date, projected, slack, True)
            )
        else:
            milestone_schedules.append(
                MilestoneSchedule(milestone.id, milestone.target_date, None, None, False)
            )

    # Hard-date constraint checks: does each targeted task finish on time?
    deadline_checks: list[DeadlineCheck] = []
    for con in plan.constraints:
        if con.type is not ConstraintType.HARD_DATE or con.hard_date is None:
            continue
        for target in con.applies_to:
            if target in task_schedules:
                projected = task_schedules[target].early_finish_date
                slack = calendar.signed_working_days(projected, con.hard_date)
                deadline_checks.append(
                    DeadlineCheck(
                        constraint_id=con.id,
                        task_id=target,
                        deadline=con.hard_date,
                        projected_finish_date=projected,
                        slack_working_days=slack,
                        met=slack >= 0,
                    )
                )

    chains = critical_paths(result)
    critical_ids = [t.id for t in plan.tasks if result.nodes[t.id].is_critical]
    project_finish = (
        calendar.nth_working_day(math.ceil(result.project_duration - _EPS) - 1)
        if result.project_duration >= 1
        else None
    )

    return Schedule(
        start_date=start_date,
        project_duration=result.project_duration,
        project_finish_date=project_finish,
        tasks=task_schedules,
        milestones=milestone_schedules,
        critical_path_ids=critical_ids,
        critical_chains=chains,
        deadline_checks=deadline_checks,
    )
