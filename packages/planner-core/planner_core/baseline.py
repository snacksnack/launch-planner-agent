"""Plan-vs-baseline comparison — the drift view Jira's timeline famously lacks.

A *baseline* is a committed plan-of-record snapshot you measure against; the
*current* plan is a later version (a manual edit today, Jira-synced actuals
later). Comparing them is a composition of two things we already built:

- **structural drift** — `diff_plans` (tasks/deps/milestones added, removed, or
  edited since the baseline), the "what changed" set the Phase 3 Status Agent
  will consume.
- **schedule variance** — `diff_schedules` (the simulator's `ScheduleDelta`):
  per-task start/finish/float movement, milestone drift, critical-path
  joiners/leavers, and the projected-finish variance.

`compare_versions` schedules both plans on the same calendar and bundles the two.
Pure and deterministic — no store, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from planner_core.diff import PlanDiff, diff_plans
from planner_core.models import Plan
from planner_core.scheduling import _DEFAULT_WEEKEND, schedule_plan
from planner_core.simulation import ScheduleDelta, diff_schedules


@dataclass(frozen=True)
class BaselineComparison:
    """The drift of a current plan against its baseline: structure + schedule."""

    plan_diff: PlanDiff
    schedule_delta: ScheduleDelta

    @property
    def is_on_track(self) -> bool:
        """True when nothing structural changed and the launch date held."""
        return self.plan_diff.is_empty and self.schedule_delta.finish_shift_days == 0

    def render(self) -> str:
        lines = [self.schedule_delta.headline]
        if self.plan_diff.is_empty:
            lines.append("No structural changes since the baseline.")
        else:
            lines.append(self.plan_diff.render(source="the baseline"))
        for note in self.schedule_delta.notes:
            lines.append(f"  - {note}")
        return "\n".join(lines)


def compare_versions(
    baseline_plan: Plan,
    current_plan: Plan,
    *,
    start_date: date,
    weekend: frozenset[int] = _DEFAULT_WEEKEND,
    blackouts: tuple[tuple[date, date], ...] = (),
) -> BaselineComparison:
    """Compare a current plan against its baseline: structural diff + schedule variance.

    Both are scheduled on the same calendar so the variance reflects plan changes,
    not a shifted start date. Task-level variance covers tasks present in both
    versions; tasks added or removed since the baseline show up in the structural
    diff. Names resolve from the current plan.
    """
    base_schedule = schedule_plan(
        baseline_plan, start_date=start_date, weekend=weekend, blackouts=blackouts
    )
    current_schedule = schedule_plan(
        current_plan, start_date=start_date, weekend=weekend, blackouts=blackouts
    )
    return BaselineComparison(
        plan_diff=diff_plans(baseline_plan, current_plan),
        schedule_delta=diff_schedules(base_schedule, current_schedule, current_plan),
    )
