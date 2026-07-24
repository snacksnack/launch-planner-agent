"""`plan` CLI — run the agent pipeline against a fixture and write a plan.

    plan breakdown fixtures/jira-cloud-migration/

Loads the PRD, team, and constraints from a fixture directory, runs the Work
Breakdown Agent, folds its output into a full `Plan` alongside the input team
and constraints, validates it deterministically, and writes `plan.json`. When a
golden expected-plan is present, it also prints a coverage comparison so the
output can be eyeballed against the hand-reviewed baseline (RC1-184).

The pure helpers (`load_fixture`, `assemble_plan`, `compare_to_golden`) are
factored out so they can be unit-tested without any LLM credentials; only
`cmd_breakdown` reaches for the live agent.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from planner_core import (
    BreakdownReport,
    Constraint,
    Plan,
    TeamMember,
    WorkBreakdown,
    build_report,
)


@dataclass
class Fixture:
    """The three input files that make up a fixture corpus."""

    directory: Path
    prd_text: str
    team: list[TeamMember]
    constraints: list[Constraint]


def load_fixture(directory: Path) -> Fixture:
    """Read prd.md / team.json / constraints.json through the P1.2 model."""
    prd_text = (directory / "prd.md").read_text()
    team = [
        TeamMember.model_validate(x) for x in json.loads((directory / "team.json").read_text())
    ]
    constraints = [
        Constraint.model_validate(x)
        for x in json.loads((directory / "constraints.json").read_text())
    ]
    return Fixture(directory=directory, prd_text=prd_text, team=team, constraints=constraints)


def assemble_plan(
    *,
    plan_id: str,
    name: str,
    source_document: str,
    breakdown: WorkBreakdown,
    team: list[TeamMember],
    constraints: list[Constraint],
) -> Plan:
    """Fold the agent's WBS into a full Plan with the input team and constraints.

    Dependencies and milestones stay empty — those are later tickets (P1.5+).
    """
    return Plan(
        id=plan_id,
        name=name,
        source_document=source_document,
        team=team,
        epics=breakdown.epics,
        tasks=breakdown.tasks,
        constraints=constraints,
    )


def compare_to_golden(produced: Plan, golden: Plan) -> str:
    """A quick eyeball comparison of counts and epic/task-name overlap.

    Deliberately shallow: ids won't match (the agent invents its own), so we
    compare on normalized names. This is a spot-check aid, not a scoring metric.
    """

    def names(items: list) -> set[str]:
        return {" ".join(i.name.lower().split()) for i in items}

    g_tasks, p_tasks = names(golden.tasks), names(produced.tasks)
    matched = g_tasks & p_tasks
    lines = [
        "Comparison vs golden baseline:",
        f"  epics:  produced {len(produced.epics)}  /  golden {len(golden.epics)}",
        f"  tasks:  produced {len(produced.tasks)}  /  golden {len(golden.tasks)}"
        f"  (name-matched {len(matched)}/{len(g_tasks)})",
    ]
    missing = sorted(g_tasks - p_tasks)
    if missing:
        lines.append("  golden tasks with no name match in output:")
        lines.extend(f"    - {n}" for n in missing)
    return "\n".join(lines)


def _write_plan(plan: Plan, out_path: Path) -> None:
    out_path.write_text(plan.model_dump_json(indent=2) + "\n")


def cmd_breakdown(args: argparse.Namespace) -> int:
    from agents import WorkBreakdownAgent

    from app.config import get_settings

    directory = Path(args.fixture)
    if not (directory / "prd.md").is_file():
        print(f"error: {directory}/prd.md not found", file=sys.stderr)
        return 2

    settings = get_settings()
    fixture = load_fixture(directory)
    out_path = Path(args.out) if args.out else directory / "plan.json"

    # Honor the project's LPA_ config: model override and, when set, the API key.
    # Without a key here the SDK falls back to its own resolution (ANTHROPIC_API_KEY,
    # `ant auth` profile, etc.).
    model = args.model or settings.anthropic_model
    client = None
    if settings.anthropic_api_key:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    agent = WorkBreakdownAgent(model=model, client=client)
    breakdown = agent.run(fixture.prd_text, fixture.team)

    plan = assemble_plan(
        plan_id=f"plan-{directory.name}",
        name=directory.name,
        source_document=str(directory / "prd.md"),
        breakdown=breakdown,
        team=fixture.team,
        constraints=fixture.constraints,
    )

    report: BreakdownReport = build_report(plan, fixture.prd_text)
    _write_plan(plan, out_path)

    print(f"wrote {out_path}")
    print(report.render())

    golden_path = directory / "golden" / "expected-plan.json"
    if golden_path.is_file():
        golden = Plan.model_validate_json(golden_path.read_text())
        print(compare_to_golden(plan, golden))

    # Errors mean the plan is malformed; surface that as a non-zero exit.
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plan", description="Launch planner agent CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    breakdown = sub.add_parser(
        "breakdown", help="Run the Work Breakdown Agent against a fixture directory."
    )
    breakdown.add_argument("fixture", help="Fixture directory (contains prd.md, team.json, ...).")
    breakdown.add_argument("--out", help="Where to write plan.json (default: <fixture>/plan.json).")
    breakdown.add_argument("--model", help="Override the Anthropic model id.")
    breakdown.set_defaults(func=cmd_breakdown)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
