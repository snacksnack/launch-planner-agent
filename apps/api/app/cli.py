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
    AddDependency,
    BreakdownReport,
    CommitRejected,
    Constraint,
    DecisionRecord,
    DelayTask,
    DependencyReport,
    MockJiraTarget,
    Plan,
    RemoveDependency,
    Scenario,
    Snapshot,
    TeamMember,
    WorkBreakdown,
    apply_keys_to_plan,
    assemble_status,
    build_decision_record,
    build_dependency_report,
    build_generation_plan,
    build_report,
    commit_baseline,
    commit_plan,
    compare_versions,
    diff_plans,
    execute_generation,
    fallback_narrative,
    monte_carlo,
    record_proposal,
    render_html,
    render_markdown,
    schedule_plan,
    simulate,
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


def decisions_sidecar(plan_path: Path) -> Path:
    """The decision-record sidecar for a plan file (plan.json -> plan.decisions.json).

    Kept beside the plan rather than inside it so the plan's content hash stays
    clean; carried onto the immutable snapshot at commit time.
    """
    return plan_path.with_suffix(".decisions.json")


def load_decision_record(plan_path: Path) -> DecisionRecord | None:
    """Load the decision-record sidecar next to a plan, if one was written."""
    sidecar = decisions_sidecar(plan_path)
    if sidecar.is_file():
        return DecisionRecord.model_validate_json(sidecar.read_text())
    return None


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
    result = agent.run(prd_text, plan.tasks, plan.constraints, plan.milestones)

    enriched = plan.model_copy(update={"dependencies": result.dependencies})
    report: DependencyReport = build_dependency_report(
        enriched, prd_text, result.rejections, result.cycle_breaks
    )

    out_path = Path(args.out) if args.out else plan_path
    out_path.write_text(enriched.model_dump_json(indent=2) + "\n")

    # Persist the build-time audit (rejected + cycle-broken edges, plus the
    # deterministic validation flags) so it survives beyond this stdout (RC1-197).
    record = build_decision_record(
        enriched, prd_text, rejected=result.rejections, cycle_breaks=result.cycle_breaks
    )
    sidecar = decisions_sidecar(out_path)
    sidecar.write_text(record.model_dump_json(indent=2) + "\n")

    print(f"wrote {out_path}")
    print(f"wrote {sidecar}")
    print(report.render())
    return 0 if report.ok else 1


def cmd_raid(args: argparse.Namespace) -> int:
    """Generate a RAID log from the PRD + the computed schedule facts."""
    from agents import RaidAgent
    from planner_core import analyze_schedule_risks, build_raid_report

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

    # Schedule facts are the agent's schedule-aware feed.
    schedule = schedule_plan(plan, start_date=date.fromisoformat(args.start_date))
    facts = analyze_schedule_risks(plan, schedule)

    settings = get_settings()
    model = args.model or settings.anthropic_model
    client = None
    if settings.anthropic_api_key:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    agent = RaidAgent(model=model, client=client)
    items = agent.run(prd_text, facts, plan.team)

    enriched = plan.model_copy(update={"raid": items})
    report = build_raid_report(enriched, prd_text)

    out_path = Path(args.out) if args.out else plan_path
    out_path.write_text(enriched.model_dump_json(indent=2) + "\n")

    print(f"wrote {out_path}")
    print(f"({len(facts)} schedule fact(s) fed to the agent)")
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


def _build_scenario(args: argparse.Namespace) -> Scenario:
    """Assemble a Scenario from repeatable --slip / --add-dep / --remove-dep flags."""
    changes: list = []
    for spec in args.slip or []:
        task_id, _, days = spec.partition(":")
        changes.append(DelayTask(task_id=task_id, days=float(days)))
    for spec in args.add_dep or []:
        pred, _, succ = spec.partition(":")
        changes.append(AddDependency(predecessor_id=pred, successor_id=succ))
    for spec in args.remove_dep or []:
        pred, _, succ = spec.partition(":")
        changes.append(RemoveDependency(predecessor_id=pred, successor_id=succ))
    return Scenario(name=args.name, changes=changes)


def cmd_simulate(args: argparse.Namespace) -> int:
    """What-if: apply a scenario, re-run CPM, and print the schedule delta (no LLM)."""
    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"error: {plan_path} not found", file=sys.stderr)
        return 2

    plan = Plan.model_validate_json(plan_path.read_text())
    blackouts = tuple(_parse_blackout(b) for b in (args.blackout or []))
    result = simulate(
        plan,
        _build_scenario(args),
        start_date=date.fromisoformat(args.start_date),
        blackouts=blackouts,
    )
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(result.delta.render())
    # Non-zero exit when the what-if pushes the launch out — useful in scripts.
    return 1 if result.delta.finish_shift_days > 0 else 0


def _sparkline(distribution: list[dict]) -> str:
    """A tiny unicode histogram of the finish-date distribution."""
    if not distribution:
        return ""
    bars = "▁▂▃▄▅▆▇█"
    peak = max(b["count"] for b in distribution) or 1
    return "".join(bars[min(len(bars) - 1, round((b["count"] / peak) * (len(bars) - 1)))]
                   for b in distribution)


def _render_forecast(result) -> str:
    """Human-readable Monte Carlo summary for the CLI."""
    def d(x) -> str:
        return x.isoformat() if x else "n/a"

    lines = [
        f"Launch-date forecast — {result.iterations} runs, seed {result.seed} "
        f"(Beta-PERT over three-point estimates)",
        f"  Point estimate (likely durations): {d(result.deterministic_finish)}",
        "",
        "  Confidence band (chance of launching on or before):",
        f"    P10  {d(result.p10)}   P50  {d(result.p50)}   "
        f"P80  {d(result.p80)}   P90  {d(result.p90)}",
        f"    → 80% confidence: on or before {d(result.p80)}.",
        f"  {_sparkline(result.distribution)}",
    ]
    ranked = [tc for tc in result.criticality if tc.criticality > 0][:8]
    if ranked:
        lines.append("")
        lines.append("  Criticality index (how often each task is on the critical path):")
        for tc in ranked:
            lines.append(f"    {tc.criticality * 100:5.0f}%  {tc.name}")
    return "\n".join(lines)


def cmd_forecast(args: argparse.Namespace) -> int:
    """Monte Carlo the launch date over the three-point estimates (deterministic, no LLM)."""
    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"error: {plan_path} not found", file=sys.stderr)
        return 2

    plan = Plan.model_validate_json(plan_path.read_text())
    blackouts = tuple(_parse_blackout(b) for b in (args.blackout or []))
    result = monte_carlo(
        plan,
        start_date=date.fromisoformat(args.start_date),
        iterations=args.iterations,
        seed=args.seed,
        blackouts=blackouts,
    )
    print(_render_forecast(result))
    return 0


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
    plan_path = Path(args.plan)
    plan = Plan.model_validate_json(plan_path.read_text())
    store = _open_store()
    snap = record_proposal(
        store,
        plan,
        now=datetime.now(UTC),
        message=args.message,
        decision_record=load_decision_record(plan_path),
    )
    print(f"recorded proposal v{snap.version} ({snap.content_hash[:12]})")
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    """Review gate: validate, then freeze an immutable plan-of-record snapshot."""
    plan_path = Path(args.plan)
    plan = Plan.model_validate_json(plan_path.read_text())
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
            decision_record=load_decision_record(plan_path),
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


def cmd_baseline(args: argparse.Namespace) -> int:
    """Commit a plan and designate it the baseline to measure drift against."""
    plan_path = Path(args.plan)
    plan = Plan.model_validate_json(plan_path.read_text())
    store = _open_store()
    try:
        snap = commit_baseline(
            store,
            plan,
            approved_by=args.by,
            note=args.note,
            now=datetime.now(UTC),
            decision_record=load_decision_record(plan_path),
        )
    except CommitRejected as exc:
        print(f"baseline rejected: {exc.reason}", file=sys.stderr)
        for issue in exc.issues:
            print(f"  ✗ [{issue.code}] {issue.message}", file=sys.stderr)
        return 1
    print(f"baselined v{snap.version} ({snap.content_hash[:12]}) — {snap.message}")
    return 0


def cmd_variance(args: argparse.Namespace) -> int:
    """Show a current plan's drift against a baseline (structure + schedule)."""
    store = _open_store()
    try:
        current = _resolve_ref(store, args.current)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.baseline:
        try:
            baseline = _resolve_ref(store, args.baseline)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        snap = store.latest_baseline()
        if snap is None:
            print("error: no baseline set — run `plan baseline` first", file=sys.stderr)
            return 2
        baseline = snap.plan

    comparison = compare_versions(
        baseline, current, start_date=date.fromisoformat(args.start_date)
    )
    print(comparison.render())
    return 0 if comparison.is_on_track else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Weekly status update: assemble facts from the changed-since diff, then narrate."""
    from datetime import date as _date

    store = _open_store()
    try:
        current = _resolve_ref(store, args.current)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    baseline_snap = None
    if args.baseline:
        try:
            baseline_plan = _resolve_ref(store, args.baseline)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        baseline_snap = store.latest_baseline()
        if baseline_snap is None:
            print("error: no baseline set — run `plan baseline` first", file=sys.stderr)
            return 2
        baseline_plan = baseline_snap.plan

    comparison = compare_versions(
        baseline_plan, current, start_date=date.fromisoformat(args.start_date)
    )
    facts = assemble_status(
        comparison,
        baseline_raid=baseline_plan.raid,
        current_raid=current.raid,
        period_label=args.period or f"as of {_date.today().isoformat()}",
        baseline_version=baseline_snap.version if baseline_snap else None,
    )

    # LLM narrative when credentials are configured; deterministic fallback otherwise.
    from app.config import get_settings

    settings = get_settings()
    if settings.anthropic_api_key:
        import anthropic
        from agents import StatusAgent

        agent = StatusAgent(
            model=args.model or settings.anthropic_model,
            client=anthropic.Anthropic(api_key=settings.anthropic_api_key),
        )
        narrative = agent.run(facts)
    else:
        narrative = fallback_narrative(facts)

    print(render_html(facts, narrative) if args.html else render_markdown(facts, narrative))
    return 0 if facts.is_on_track else 1


def cmd_jira(args: argparse.Namespace) -> int:
    """Generate Jira issues from a plan — mock preview by default, real behind a gate."""
    from app.config import get_settings

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"error: {plan_path} not found", file=sys.stderr)
        return 2

    plan = Plan.model_validate_json(plan_path.read_text())
    schedule = schedule_plan(plan, start_date=date.fromisoformat(args.start_date))
    settings = get_settings()
    project = args.project or settings.jira_project_key
    gen = build_generation_plan(plan, schedule, project_key=project)
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    if not args.real:
        # Mock: preview exactly what real mode would do — no credentials, no writes.
        result = execute_generation(gen, MockJiraTarget(project_key=project), only=only)
        print(gen.render())
        print(
            f"\n[mock] would create {len(result.created)}, update {len(result.updated)}, "
            f"link {result.linked} — no writes. Re-run with --real --confirm to apply."
        )
        return 0

    # Real mode: gated on credentials and an explicit --confirm.
    if not settings.has_jira_credentials:
        print(
            "error: real mode needs LPA_JIRA_BASE_URL, LPA_JIRA_EMAIL, LPA_JIRA_API_TOKEN",
            file=sys.stderr,
        )
        return 2
    if not args.confirm:
        print(
            "refusing to write to Jira without --confirm (real mode creates issues).",
            file=sys.stderr,
        )
        return 2

    from app.jira_client import RealJiraTarget

    target = RealJiraTarget(
        base_url=settings.jira_base_url,
        email=settings.jira_email,
        api_token=settings.jira_api_token,
    )
    try:
        result = execute_generation(gen, target, only=only)
    finally:
        target.close()

    updated = apply_keys_to_plan(plan, result.key_by_local_id)
    out_path = Path(args.out) if args.out else plan_path
    out_path.write_text(updated.model_dump_json(indent=2) + "\n")
    print(
        f"[real] created {len(result.created)}, updated {len(result.updated)}, "
        f"link {result.linked}"
    )
    print(f"wrote {out_path} with jira_key mappings (idempotent on re-run)")
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

    raid = sub.add_parser(
        "raid", help="Generate a RAID log from the PRD + computed schedule facts."
    )
    raid.add_argument("plan", help="Path to a scheduled plan.json.")
    raid.add_argument("--start-date", required=True, help="Project start date (YYYY-MM-DD).")
    raid.add_argument("--prd", help="PRD path (default: the plan's source_document).")
    raid.add_argument("--out", help="Where to write the enriched plan (default: in place).")
    raid.add_argument("--model", help="Override the Anthropic model id.")
    raid.set_defaults(func=cmd_raid)

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

    simulate_cmd = sub.add_parser(
        "simulate",
        help="What-if: apply a scenario (slips, dep edits) and print the schedule delta.",
    )
    simulate_cmd.add_argument("plan", help="Path to a plan.json.")
    simulate_cmd.add_argument(
        "--start-date", required=True, help="Project start date (YYYY-MM-DD)."
    )
    simulate_cmd.add_argument("--name", help="Optional scenario name.")
    simulate_cmd.add_argument(
        "--slip", action="append", metavar="TASK_ID:DAYS",
        help="Slip a task by N working days. Repeatable.",
    )
    simulate_cmd.add_argument(
        "--add-dep", action="append", metavar="PRED:SUCC",
        help="Add a hypothetical dependency edge. Repeatable.",
    )
    simulate_cmd.add_argument(
        "--remove-dep", action="append", metavar="PRED:SUCC",
        help="Remove an existing dependency edge. Repeatable.",
    )
    simulate_cmd.add_argument(
        "--blackout", action="append", metavar="START:END",
        help="Freeze/blackout window as START:END (YYYY-MM-DD:YYYY-MM-DD). Repeatable.",
    )
    simulate_cmd.set_defaults(func=cmd_simulate)

    forecast = sub.add_parser(
        "forecast",
        help="Monte Carlo the launch date over three-point estimates (P50/P80/P90 + criticality).",
    )
    forecast.add_argument("plan", help="Path to a plan.json.")
    forecast.add_argument("--start-date", required=True, help="Project start date (YYYY-MM-DD).")
    forecast.add_argument(
        "--iterations", type=int, default=1000, help="Number of Monte Carlo runs (default 1000)."
    )
    forecast.add_argument(
        "--seed", type=int, default=0, help="RNG seed — a run is reproducible for a fixed seed."
    )
    forecast.add_argument(
        "--blackout", action="append", metavar="START:END",
        help="Freeze/blackout window as START:END (YYYY-MM-DD:YYYY-MM-DD). Repeatable.",
    )
    forecast.set_defaults(func=cmd_forecast)

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

    # --- baselines & plan-vs-actual (RC1-192) ---
    baseline = sub.add_parser(
        "baseline", help="Commit a plan and designate it the baseline to measure against."
    )
    baseline.add_argument("plan", help="Path to the reviewed plan.json.")
    baseline.add_argument("--by", required=True, help="Approver name (the human sign-off).")
    baseline.add_argument(
        "--note", required=True, help="Why this baseline (e.g. 'initial plan', 're-baseline: ...')."
    )
    baseline.set_defaults(func=cmd_baseline)

    variance = sub.add_parser(
        "variance", help="Show a plan's drift against a baseline (structure + schedule)."
    )
    variance.add_argument("current", help="Current plan ref (version, hash, or file).")
    variance.add_argument(
        "--baseline", metavar="REF", help="Baseline ref (default: the latest baseline)."
    )
    variance.add_argument("--start-date", required=True, help="Project start date (YYYY-MM-DD).")
    variance.set_defaults(func=cmd_variance)

    # --- weekly status update (RC1-194) ---
    status = sub.add_parser(
        "status", help="Weekly exec status update from the changed-since diff (Markdown/HTML)."
    )
    status.add_argument("current", help="Current plan ref (version, hash, or file).")
    status.add_argument("--start-date", required=True, help="Project start date (YYYY-MM-DD).")
    status.add_argument("--baseline", metavar="REF", help="Baseline ref (default: latest).")
    status.add_argument("--period", help="Period label (default: 'as of <today>').")
    status.add_argument("--html", action="store_true", help="Render HTML instead of Markdown.")
    status.add_argument("--model", help="Override the Anthropic model id (LLM narrative).")
    status.set_defaults(func=cmd_status)

    # --- Jira ticket generation (RC1-193) ---
    jira = sub.add_parser(
        "jira", help="Generate Jira issues from a plan (mock preview by default)."
    )
    jira.add_argument("plan", help="Path to a scheduled plan.json.")
    jira.add_argument("--start-date", required=True, help="Project start date (YYYY-MM-DD).")
    jira.add_argument("--project", help="Jira project key (default: LPA_JIRA_PROJECT_KEY).")
    jira.add_argument(
        "--only", metavar="IDS", help="Comma-separated entity ids to include (partial approval)."
    )
    jira.add_argument(
        "--real", action="store_true", help="Write to Jira (needs credentials + --confirm)."
    )
    jira.add_argument(
        "--confirm", action="store_true", help="Required with --real to actually create issues."
    )
    jira.add_argument("--out", help="Where to write the plan with jira_key mappings (real mode).")
    jira.set_defaults(func=cmd_jira)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
