"""`plan.forecast` — the launch date as a probability, not a point.

*"80% chance of launching on or before Oct 23."*

Three decisions shape this module.

**The optimism gap is returned as a number, not a label.** The deterministic
schedule reports one date from each task's most-likely estimate. Because the
project finish is a max over converging paths, that date is provably *earlier*
than the expected finish (Jensen's inequality — see docs/forecasting.md). On the
golden the plan says Oct 12 and only ~19% of runs achieve it. Handed both
figures unlabelled, a model reports the earlier one. So the deterministic date
ships with `deterministic_confidence`: the actual share of runs that finished on
or before it. A reader does not have to be told it is optimistic; they can see
how optimistic.

**No histogram.** `MonteCarloResult.distribution` is a per-date bucket list meant
to draw a chart. It is a third of the payload and answers nothing a caller asked.
The band, the mean, and the criticality index are the answer.

**`correlation` is not exposed.** It defaults to 0 everywhere and ADR-0026
deliberately kept it out of the UI, because "how correlated is your project?" has
no units anybody can estimate. A model is likelier than a human to set a
plausible-sounding value, and the failure is silent: numbers matching nothing on
the dashboard. It is echoed in the response so a stored forecast still records
how it was produced, and it can be exposed later without changing any existing
field.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from mcp.server import MCPServer
from planner_core import monte_carlo
from pydantic import BaseModel, Field

from mcp_server.errors import InvalidArgument, legible_errors
from mcp_server.resolve import resolve_plan_ref
from mcp_server.schemas import PlanRef, start_date_or_default

# Mirrors the bounds `/api/forecast` already enforces, so the two surfaces
# reject the same inputs rather than one being stricter by accident.
MIN_ITERATIONS = 100
MAX_ITERATIONS = 5_000
DEFAULT_ITERATIONS = 1_000

DEFAULT_TOP_TASKS = 10
MAX_TOP_TASKS = 50


class CriticalityEntry(BaseModel):
    """How often a task landed on the critical path across the sampled runs."""

    task_id: str
    name: str
    owner_name: str | None = None
    criticality: float = Field(
        description="0..1 — the share of runs in which this task was critical."
    )


class LaunchForecast(BaseModel):
    ref: PlanRef
    start_date: date

    iterations: int
    seed: int = Field(
        description=(
            "Echoed even when it defaulted. The forecast is reproducible for a fixed "
            "seed, and a number nobody can reproduce is a number nobody can defend."
        )
    )
    correlation: float = Field(
        description=(
            "How strongly task durations move together. Always 0 in this version: "
            "durations are sampled independently, matching the published dashboard."
        )
    )

    p50: date | None
    p80: date | None
    p90: date | None
    mean_working_days: float

    deterministic_date: date | None = Field(
        description=(
            "The single-point date from the most-likely estimates — what the plan "
            "says. Optimistic by construction; read it with deterministic_confidence."
        )
    )
    deterministic_confidence: float = Field(
        description=(
            "Share of runs that finished on or before the deterministic date. Low "
            "values mean the plan's own date is unlikely, not that the model is wrong."
        )
    )

    summary: str = Field(description="One sentence a reader can quote directly.")

    criticality: list[CriticalityEntry]
    criticality_truncated: bool
    tasks_ever_critical: int = Field(
        description="How many tasks were critical in at least one run."
    )

    computed_at: datetime


def _confidence_at_or_before(distribution: list[dict], target: date | None, runs: int) -> float:
    """Share of sampled runs that finished on or before `target`."""
    if target is None or runs <= 0:
        return 0.0
    iso = target.isoformat()
    hits = sum(bucket["count"] for bucket in distribution if bucket["date"] <= iso)
    return round(hits / runs, 4)


def register(server: MCPServer) -> None:
    @server.tool(
        name="plan.forecast",
        description=(
            "Give a launch date as a probability rather than a single day: the P50, P80 "
            "and P90 confidence band, produced by sampling every task's three-point "
            "estimate and re-running the critical-path engine many times. Use this for "
            "'when will this realistically land', 'how confident are we in the date', or "
            "'what date can I commit to'.\n\n"
            "The plan's own single-point date is returned as `deterministic_date` with "
            "`deterministic_confidence` — the share of runs that actually achieved it. "
            "That number is usually low, because a project finish is a maximum over "
            "converging paths and so the single-point date is biased early. Report the "
            "band, not the deterministic date, when someone asks when the plan lands.\n\n"
            "Also returns a criticality index: how *often* each task was on the critical "
            "path across the runs. That is a different question from plan.critical_path, "
            "which returns the chains driving the date in one deterministic pass. Use "
            "this tool for 'what is most likely to delay us' and plan.critical_path for "
            "'what is on the critical path'.\n\n"
            "Reproducible: a fixed `seed` gives identical numbers, and the seed is "
            "always echoed back. `iterations` trades precision for time (100–5000, "
            "default 1000). `ref` and `start` select and schedule the plan as in "
            "plan.get. Read-only: this samples in memory and changes nothing."
        ),
    )
    @legible_errors
    def plan_forecast(
        ref: str | None = None,
        start: str | None = None,
        iterations: int = DEFAULT_ITERATIONS,
        seed: int = 0,
        top_tasks: int = DEFAULT_TOP_TASKS,
    ) -> LaunchForecast:
        if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
            raise InvalidArgument(
                f"iterations must be between {MIN_ITERATIONS} and {MAX_ITERATIONS} "
                f"(got {iterations}). The default of {DEFAULT_ITERATIONS} is what the "
                "dashboard uses."
            )
        if not 1 <= top_tasks <= MAX_TOP_TASKS:
            raise InvalidArgument(
                f"top_tasks must be between 1 and {MAX_TOP_TASKS} (got {top_tasks})."
            )

        resolved = resolve_plan_ref(ref)
        start_date = start_date_or_default(start)
        result = monte_carlo(
            resolved.plan,
            start_date=start_date,
            iterations=iterations,
            seed=seed,
            # Deliberately not a parameter — see the module docstring.
            correlation=0.0,
        )

        owners = {
            member.id: member.name for member in resolved.plan.team
        }
        owner_of = {task.id: owners.get(task.owner_id) for task in resolved.plan.tasks}

        ever_critical = [entry for entry in result.criticality if entry.criticality > 0]
        top = ever_critical[:top_tasks]

        confidence = _confidence_at_or_before(
            result.distribution, result.deterministic_finish, result.iterations
        )

        if result.p80 is None or result.deterministic_finish is None:
            summary = "This plan has no schedulable work, so there is no launch date to forecast."
        else:
            summary = (
                f"80% chance of launching on or before {result.p80.isoformat()} "
                f"(P50 {result.p50.isoformat()}, P90 {result.p90.isoformat()}). "
                f"The plan's own date of {result.deterministic_finish.isoformat()} was "
                f"achieved in {confidence:.0%} of {result.iterations} runs, so treat it "
                "as optimistic rather than as the date."
            )

        return LaunchForecast(
            ref=PlanRef.of(resolved),
            start_date=result.start_date,
            iterations=result.iterations,
            seed=result.seed,
            correlation=result.correlation,
            p50=result.p50,
            p80=result.p80,
            p90=result.p90,
            mean_working_days=result.mean_working_days,
            deterministic_date=result.deterministic_finish,
            deterministic_confidence=confidence,
            summary=summary,
            criticality=[
                CriticalityEntry(
                    task_id=entry.task_id,
                    name=entry.name,
                    owner_name=owner_of.get(entry.task_id),
                    criticality=entry.criticality,
                )
                for entry in top
            ],
            criticality_truncated=len(ever_critical) > len(top),
            tasks_ever_critical=len(ever_critical),
            computed_at=datetime.now(UTC),
        )
