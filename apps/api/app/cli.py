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
from datetime import UTC, date, datetime
from pathlib import Path

from planner_core import (
    BreakdownReport,
    CommitRejected,
    Constraint,
    DependencyReport,
    Plan,
    Snapshot,
    TeamMember,
    WorkBreakdown,
    build_dependency_report,
    build_report,
    commit_plan,
    diff_plans,
    record_proposal,
    schedule_plan,
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


def resolve_prd(plan: Plan, plan_path: Path, explicit: str | None) -> str | None:
    """Find the PRD text for a plan: --prd, then source_document, then sibling."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if plan.source_document:
        candidates.append(Path(plan.source_document))
    candidates.append(plan_path.parent / "prd.md")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text()
    return None


def cmd_dependencies(args: argparse.Namespace) -> int:
    from agents import DependencyAgent

    from app.config import get_settings

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"error: {plan_path} not found", file=sys.stderr)
        return 2

    plan = Plan.model_validate_json(plan_path.read_text())
    prd_text = resolve_prd(plan, plan_path, args.prd)
    if prd_text is None:
        print(
            "error: could not locate the PRD (pass --prd, or ensure source_document resolves)",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    model = args.model or settings.anthropic_model
    client = None
    if settings.anthropic_api_key:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    agent = DependencyAgent(model=model, client=client)
    result = agent.run(prd_text, plan.tasks, plan.constraints)

    enriched = plan.model_copy(update={"dependencies": result.dependencies})
    report: DependencyReport = build_dependency_report(
        enriched, prd_text, result.rejections, result.cycle_breaks
    )

    out_path = Path(args.out) if args.out else plan_path
    out_path.write_text(enriched.model_dump_json(indent=2) + "\n")

    print(f"wrote {out_path}")
    print(report.render())
    return 0 if report.ok else 1


def _parse_blackout(spec: str) -> tuple[date, date]:
    start, _, end = spec.partition(":")
    return date.fromisoformat(start), date.fromisoformat(end)


def cmd_schedule(args: argparse.Namespace) -> int:
    """Deterministic CPM schedule — no LLM, no credentials."""
    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"error: {plan_path} not found", file=sys.stderr)
        return 2

    plan = Plan.model_validate_json(plan_path.read_text())
    blackouts = tuple(_parse_blackout(b) for b in (args.blackout or []))

    schedule = schedule_plan(
        plan, start_date=date.fromisoformat(args.start_date), blackouts=blackouts
    )
    print(schedule.render())
    return 0 if schedule.meets_all_deadlines else 1


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


# --- plan-of-record store (RC1-189) ---------------------------------------


def _open_store():
    from app.config import get_settings
    from app.store import SQLiteEventStore

    return SQLiteEventStore(get_settings().sqlite_path)


def _resolve_ref(store, ref: str) -> Plan:
    """Resolve a plan reference: a snapshot version (int), a content hash, or a file."""
    if ref.isdigit():
        snap = store.get_by_version(int(ref))
        if snap is None:
            raise KeyError(f"no snapshot at version {ref}")
        return snap.plan
    path = Path(ref)
    if path.is_file():
        return Plan.model_validate_json(path.read_text())
    snap = store.get_by_hash(ref)
    if snap is None:
        raise KeyError(f"no snapshot for reference {ref!r}")
    return snap.plan


def _snapshot_line(s: Snapshot) -> str:
    who = f" by {s.approved_by}" if s.approved_by else ""
    msg = f" — {s.message}" if s.message else ""
    return (
        f"  v{s.version} [{s.kind.value}] {s.content_hash[:12]}{who} "
        f"· {s.created_at.date().isoformat()}{msg}"
    )


def cmd_propose(args: argparse.Namespace) -> int:
    """Record an agent proposal so a later commit can be diffed against it."""
    plan = Plan.model_validate_json(Path(args.plan).read_text())
    store = _open_store()
    snap = record_proposal(store, plan, now=datetime.now(UTC), message=args.message)
    print(f"recorded proposal v{snap.version} ({snap.content_hash[:12]})")
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    """Review gate: validate, then freeze an immutable plan-of-record snapshot."""
    plan = Plan.model_validate_json(Path(args.plan).read_text())
    store = _open_store()

    source_hash = None
    if args.from_proposal:
        try:
            source_hash = content_hash_of(_resolve_ref(store, args.from_proposal))
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        snap = commit_plan(
            store,
            plan,
            approved_by=args.by,
            now=datetime.now(UTC),
            message=args.message,
            source_proposal_hash=source_hash,
        )
    except CommitRejected as exc:
        print(f"commit rejected: {exc.reason}", file=sys.stderr)
        for issue in exc.issues:
            print(f"  ✗ [{issue.code}] {issue.message}", file=sys.stderr)
        return 1

    print(f"committed v{snap.version} ({snap.content_hash[:12]}) approved by {snap.approved_by}")
    if args.start_date:
        schedule = schedule_plan(plan, start_date=date.fromisoformat(args.start_date))
        print(schedule.render())
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    store = _open_store()
    snapshots = store.history()
    if not snapshots:
        print("no snapshots yet")
        return 0
    print(f"{len(snapshots)} snapshot(s):")
    for s in snapshots:
        print(_snapshot_line(s))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _open_store()
    try:
        plan = _resolve_ref(store, args.ref)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(plan.model_dump_json(indent=2))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    store = _open_store()
    try:
        base = _resolve_ref(store, args.base)
        revised = _resolve_ref(store, args.revised)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(diff_plans(base, revised).render())
    return 0


def content_hash_of(plan: Plan) -> str:
    from planner_core import content_hash

    return content_hash(plan)


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

    dependencies = sub.add_parser(
        "dependencies", help="Infer and validate dependencies over an existing plan.json."
    )
    dependencies.add_argument("plan", help="Path to a plan.json produced by `plan breakdown`.")
    dependencies.add_argument("--prd", help="PRD path (default: the plan's source_document).")
    dependencies.add_argument("--out", help="Where to write the enriched plan (default: in place).")
    dependencies.add_argument("--model", help="Override the Anthropic model id.")
    dependencies.set_defaults(func=cmd_dependencies)

    schedule = sub.add_parser(
        "schedule", help="Compute the CPM schedule for a plan.json (deterministic, no LLM)."
    )
    schedule.add_argument("plan", help="Path to a plan.json.")
    schedule.add_argument(
        "--start-date", required=True, help="Project start date (YYYY-MM-DD)."
    )
    schedule.add_argument(
        "--blackout",
        action="append",
        metavar="START:END",
        help="Freeze/blackout window as START:END (YYYY-MM-DD:YYYY-MM-DD). Repeatable.",
    )
    schedule.set_defaults(func=cmd_schedule)

    # --- plan-of-record store (RC1-189) ---
    propose = sub.add_parser("propose", help="Record an agent proposal in the store.")
    propose.add_argument("plan", help="Path to a plan.json.")
    propose.add_argument("-m", "--message", help="Optional note.")
    propose.set_defaults(func=cmd_propose)

    commit = sub.add_parser(
        "commit", help="Validate and commit an immutable plan-of-record snapshot."
    )
    commit.add_argument("plan", help="Path to the reviewed plan.json.")
    commit.add_argument("--by", required=True, help="Approver name (the human sign-off).")
    commit.add_argument("-m", "--message", help="Commit message.")
    commit.add_argument(
        "--from", dest="from_proposal", metavar="REF",
        help="The proposal this derives from (version, hash, or file) — enables the audit diff.",
    )
    commit.add_argument("--start-date", help="If set, re-run the schedule (YYYY-MM-DD).")
    commit.set_defaults(func=cmd_commit)

    history = sub.add_parser("history", help="List the plan snapshot history.")
    history.set_defaults(func=cmd_history)

    show = sub.add_parser("show", help="Print a snapshot's plan (by version, hash, or file).")
    show.add_argument("ref", help="Snapshot version, content hash, or a plan.json path.")
    show.set_defaults(func=cmd_show)

    diff = sub.add_parser("diff", help="Diff two plans (human-vs-agent audit trail).")
    diff.add_argument("base", help="Base ref (version, hash, or file) — e.g. the agent proposal.")
    diff.add_argument("revised", help="Revised ref (version, hash, or file) — e.g. the commit.")
    diff.set_defaults(func=cmd_diff)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
