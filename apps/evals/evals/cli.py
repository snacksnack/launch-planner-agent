"""`evals` CLI — run a subject against its cases, and read a run back.

    uv run evals run health
    uv run evals report health-20260813T174233Z

Judge calibration (RC1-250) is the other half:

    uv run evals seed          # generate the seed set (billed, run once)
    uv run evals label         # score it by hand — free, resumable
    uv run evals judge         # score the same set with the judge (billed)
    uv run evals calibrate     # per-dimension agreement, and what may gate

Exit codes are CI-shaped from the start, because RC1-255 turns this into a gate
and a gate that always exits 0 is a report nobody opens:

    0  every case passed
    1  at least one case failed its characteristics
    2  at least one case errored — the subject did not produce an output to score

2 outranks 1 deliberately. "The thing under test is broken" and "the thing under
test answered badly" need different people looking at them, and collapsing both
into one non-zero code loses that.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_evals import agreement, construct, judge, labelling, llmobs
from agent_evals.record import RunRecord, RunStore, new_run_id
from agent_evals.rubric import JUDGED, JUDGED_KEYS, RUBRIC_VERSION
from agent_evals.runner import store_from_env
from agent_evals.seeds import LabelStore, SeedStore, unlabelled
from agents.status import DEFAULT_MODEL
from app.config import get_settings

from evals import budget, seedgen
from evals.config import LABELS_PATH, SEEDS_PATH, get_eval_settings
from evals.subjects import BILLED, SUBJECTS

#: A dimension gates only if the point estimate clears the floor *and* the risk
#: of the true value being below it is under this. RC1-250 measured 0.66 with
#: 34% below and the honest call was not to gate; encoding that beats
#: re-litigating it each time.
_MAX_RISK_OF_NOT_GATING = 0.20

_PASS = "pass"
_FAIL = "FAIL"


def _store(args: argparse.Namespace):
    """An explicit --runs-path is a request to work with a local file and wins.
    Otherwise `agent_evals.runner.store_from_env` (RC1-262): the shared
    Postgres store when EVAL_DATABASE_URL is in the process environment, else
    this repo's configured local JSONL."""
    if args.runs_path:
        return RunStore(Path(args.runs_path))
    return store_from_env(get_eval_settings().runs_path)


def cmd_run(args: argparse.Namespace) -> int:
    subject = SUBJECTS.get(args.subject)
    if subject is None:
        known = ", ".join(sorted(SUBJECTS)) or "(none registered)"
        print(f"unknown subject {args.subject!r}. Known subjects: {known}", file=sys.stderr)
        return 2

    # A subject may declare a precondition (credentials, a reachable service).
    # Optional and duck-typed: `getattr` rather than a base class, because one
    # subject needing it is not two implementations to generalise from.
    check = getattr(subject, "preflight", None)
    if check is not None:
        try:
            check()
        except Exception as exc:
            print(f"{args.subject} cannot run: {exc}", file=sys.stderr)
            return 2

    if args.subject in BILLED:
        # Said out loud rather than prompted for: this has to run unattended in
        # CI, and a subject that quietly spends tokens is the kind of surprise
        # RC1-254's budgets exist to prevent.
        print(
            f"{args.subject} drives a real model — {len(subject.CASES)} cases will spend tokens.",
            file=sys.stderr,
        )
        # RC1-322: billed spend is traced spend. Free subjects stay untraced —
        # enable only here, and llmobs.case() no-ops when tracing is off.
        # Since agent-evals v0.4.1 (RC1-331), enable() itself restricts ddtrace
        # patching to the anthropic integration — the RC1-326 local DD_TRACE_*
        # opt-outs that used to sit here moved into the harness.
        llmobs.enable("launch-planner", service="evals")

    started_at = datetime.now(UTC)
    results = []
    # One scratch root for the whole run so a case cannot inherit the previous
    # case's store — cross-case leakage is the classic way a suite goes green
    # for the wrong reason.
    with TemporaryDirectory(prefix="evals-") as tmp:
        for index, case in enumerate(subject.CASES):
            case_root = Path(tmp) / f"{index:03d}-{case.id}"
            case_root.mkdir(parents=True)
            with llmobs.case(case.id) as traced:
                result = subject.run(case, case_root)
                traced.record(result)
            results.append(result)

    record = RunRecord(
        run_id=new_run_id(subject.NAME, started_at),
        subject_version=subject.version(),
        started_at=started_at,
        finished_at=datetime.now(UTC),
        results=results,
    )
    _store(args).append(record)

    _print_report(record, verbose=args.verbose)
    print(f"\nrun {record.run_id} recorded")
    if record.errored:
        return 2
    return 1 if record.failed else 0


def cmd_report(args: argparse.Namespace) -> int:
    store = _store(args)
    record = store.get(args.run_id) if args.run_id else store.latest(args.subject)
    if record is None:
        which = f"run {args.run_id!r}" if args.run_id else "any run"
        where = getattr(store, "path", None) or "the shared store (EVAL_DATABASE_URL)"
        print(f"no record found for {which} in {where}", file=sys.stderr)
        return 2

    _print_report(record, verbose=not args.quiet)
    if record.errored:
        return 2
    return 1 if record.failed else 0


def _print_report(record: RunRecord, *, verbose: bool) -> None:
    version = record.subject_version
    model = version.model or "none (deterministic)"
    prompt = version.prompt_version or "none"
    print(f"{record.run_id}")
    print(f"  subject       {version.subject} @ {version.code_version}")
    print(f"  model         {model}")
    print(f"  prompt        {prompt}")
    print(f"  started       {record.started_at.isoformat()}")
    print(
        f"  cases         {record.passed}/{len(record.results)} passed"
        + (f", {record.errored} errored" if record.errored else "")
    )
    # Printed even when it is zero: "this subject costs nothing to evaluate" is
    # a claim worth being able to make from the record rather than from memory.
    print(f"  cost          ${record.total_cost_usd} · {record.total_latency_ms:.0f} ms total")
    # Only when something actually measured it — a subject that checks no claims
    # must not report 0.0%, which reads as "nothing was hallucinated" rather
    # than "nothing was checked" (RC1-251).
    _print_budget(record)
    if (rate := record.hallucination_rate) is not None:
        checked = sum(r.observations.get("claims_checked", 0) for r in record.results)
        flagged = sum(r.observations.get("violations", 0) for r in record.results)
        print(f"  hallucination {rate:.1%}  ({flagged} unsupported of {checked} checkable claims)")

    for result in record.results:
        if result.error:
            print(f"\n  ERROR {result.case_id}")
            print(f"    {result.error}")
            continue
        status = _PASS if result.passed else _FAIL
        print(f"\n  {status} {result.case_id}  ({result.usage.latency_ms:.0f} ms)")
        for characteristic in result.characteristics:
            if characteristic.passed and not verbose:
                continue
            # An advisory miss gets its own mark and an explicit label: RC1-255
            # requires the output to distinguish what gates from what merely
            # reports, so a reader can never mistake one for the other.
            if characteristic.advisory:
                mark = "✓" if characteristic.passed else "~"
                label = "" if characteristic.passed else " [advisory]"
            else:
                mark = "✓" if characteristic.passed else "✗"
                label = ""
            print(f"    {mark} {characteristic.name}{label}: {characteristic.detail}")

    _print_confusion(record)


def _print_budget(record: RunRecord) -> None:
    """Cost and quality on one view.

    The decision a budget informs is "can this move to a cheaper model", and
    that is only answerable if the quality delta and the cost delta are visible
    together — which is why this prints beside the case count rather than in a
    report of its own (RC1-254).
    """
    ceiling = budget.for_subject(record.subject_version.subject)
    if ceiling is None:
        return
    breaches = ceiling.breaches(record.total_cost_usd, record.total_latency_ms)
    if not breaches:
        print(
            f"  budget        within ceiling (${ceiling.max_cost_usd} · "
            f"{ceiling.max_latency_ms / 1000:.0f}s)"
        )
        return
    # Advisory: a run that cost more has not produced a wrong answer. Loud, but
    # never a build failure — RC1-255 gates on correctness.
    for breach in breaches:
        print(f"  budget        ~ BREACH [advisory]: {breach}")
    print(f"                ceiling set from: {ceiling.note}")


def _print_confusion(record: RunRecord) -> None:
    """Which tool was chosen when the intended one wasn't.

    A pass rate says something broke; this says *which description competed*,
    which is the part you can act on. Only mis-routes are listed — a matrix of
    mostly-zeroes for nine tools is harder to read than the handful of confusion
    pairs that actually occurred (RC1-249).
    """
    confusions: dict[tuple[str, str], int] = {}
    routed = 0
    for result in record.results:
        expected = result.observations.get("expected_tool")
        if expected is None:
            continue
        routed += 1
        actual = result.observations.get("actual_tool") or "(no tool called)"
        if actual != expected:
            confusions[(expected, actual)] = confusions.get((expected, actual), 0) + 1

    if not routed:
        return

    reached = sum(
        1
        for r in record.results
        if r.observations.get("expected_tool") in (r.observations.get("tools_called") or [])
    )
    direct = routed - sum(confusions.values())
    print(f"\n  routing      {reached}/{routed} called the intended tool")
    print(f"  directness   {direct}/{routed} went straight to it, no preparatory call")
    if not confusions:
        return
    # First pick only. A wrong *first* pick is what points at a competing
    # description; where the intended tool was still reached afterwards, the
    # row is a detour rather than a miss, and is labelled as such.
    print("  confusion    intended -> chosen first")
    for (expected, actual), count in sorted(confusions.items(), key=lambda kv: -kv[1]):
        # Split the row: a detour still reached the intended tool, a miss never
        # did. Labelling the whole row by whether *any* case detoured would call
        # a real miss a detour — which is the opposite of actionable.
        detours = sum(
            1
            for r in record.results
            if r.observations.get("expected_tool") == expected
            and r.observations.get("actual_tool") == actual
            and expected in (r.observations.get("tools_called") or [])
        )
        misses = count - detours
        parts = []
        if misses:
            parts.append(f"{misses} miss" + ("es" if misses > 1 else ""))
        if detours:
            parts.append(f"{detours} detour" + ("s" if detours > 1 else ""))
        print(f"               {expected} -> {actual}  ({', '.join(parts)})")


def cmd_seed(args: argparse.Namespace) -> int:
    """Generate the seed set. Billed, and refuses to run twice by accident."""
    store = SeedStore(Path(args.seeds_path or SEEDS_PATH))
    if store.all() and not args.force:
        print(
            f"{store.path} already holds {len(store.all())} seed(s). Regenerating would "
            "invalidate every label collected against them — pass --force if that is "
            "really what you want.",
            file=sys.stderr,
        )
        return 2
    try:
        seedgen.preflight()
    except Exception as exc:
        print(f"cannot generate: {exc}", file=sys.stderr)
        return 2

    fact_sets = len(seedgen.FACT_SETS)
    print(
        f"generating {fact_sets * 3} seeds from {fact_sets} fact sets (2 of 3 variants are billed)…"
    )
    for seed in seedgen.generate():
        store.append(seed)
        print(f"  {seed.id}")
    print(f"\nwrote {store.path}")
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    """Score the seed set by hand. Free, resumable, and the actual deliverable."""
    seeds = SeedStore(Path(args.seeds_path or SEEDS_PATH)).all()
    if not seeds:
        print("no seeds yet — run `evals seed` first", file=sys.stderr)
        return 2
    if args.limit:
        # A careful pass over fewer items beats a fast pass over many: the first
        # attempt at this set was 36 seeds x 4 dimensions = 144 judgements, and
        # the labeller said afterwards not to trust it. `n` is reported, so a
        # smaller careful set is honest where a larger careless one is not.
        seeds = labelling.shuffled(seeds)[: args.limit]

    dimension = None
    if args.dimension:
        dimension = next((d for d in JUDGED if d.key == args.dimension), None)
        if dimension is None:
            print(
                f"unknown dimension {args.dimension!r}. One of: {', '.join(JUDGED_KEYS)}",
                file=sys.stderr,
            )
            return 2

    store = LabelStore(Path(args.labels_path or LABELS_PATH))
    existing = store.by_scorer(args.scorer)
    have = {seed_id: dict(label.scores) for seed_id, label in existing.items()}

    if dimension:
        todo = [s for s in labelling.shuffled(seeds) if dimension.key not in have.get(s.id, {})]
    else:
        todo = labelling.shuffled(unlabelled(seeds, existing))

    if existing:
        print(f"scorer {args.scorer!r}: {len(existing)} seed(s) already have some scores.")

    labelling.run_session(
        todo,
        store,
        read=input,
        write=print,
        dimension=dimension,
        scorer=args.scorer,
        existing=have,
    )
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    """Score the same seeds with the judge. Billed."""
    seeds = SeedStore(Path(args.seeds_path or SEEDS_PATH)).all()
    if not seeds:
        print("no seeds yet — run `evals seed` first", file=sys.stderr)
        return 2
    # RC1-261: the library takes a resolved key and a resolved model rather than
    # reaching for configuration itself. Resolving them is this repo's job —
    # `LPA_` is our prefix, and `agent_evals` runs in three repos that each spell
    # it differently.
    settings = get_settings()
    try:
        client = judge.client_for(settings.anthropic_api_key)
    except Exception as exc:
        print(f"cannot run the judge: {exc} (set LPA_ANTHROPIC_API_KEY)", file=sys.stderr)
        return 2
    model = settings.anthropic_model or DEFAULT_MODEL
    llmobs.enable("launch-planner", service="judge")  # RC1-322: the judge bills too

    store = LabelStore(Path(args.labels_path or LABELS_PATH))
    done = store.by_scorer(judge.JUDGE_VERSION)
    todo = [seed for seed in seeds if seed.id not in done] if not args.force else seeds
    print(f"{judge.JUDGE_VERSION} scoring {len(todo)} seed(s)…")
    failures = []
    for seed in todo:
        # One unscoreable seed must not abandon a run that has already spent
        # money on the rest. Re-running resumes, so a transient failure costs
        # one seed rather than the whole set.
        try:
            store.append(judge.score(seed, client, model))
            print(f"  {seed.id}")
        except Exception as exc:
            failures.append((seed.id, str(exc)))
            print(f"  {seed.id}  FAILED", file=sys.stderr)

    if failures:
        print(f"\n{len(failures)} seed(s) could not be scored:", file=sys.stderr)
        for seed_id, message in failures:
            print(f"  {seed_id}: {message}", file=sys.stderr)
        print("re-run to retry only these", file=sys.stderr)
        return 2
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """The story's headline: per-dimension agreement, and what earns gating."""
    store = LabelStore(Path(args.labels_path or LABELS_PATH))
    left_name = args.scorer
    right_name = args.against or judge.JUDGE_VERSION
    human = {k: v.scores for k, v in store.by_scorer(left_name).items()}
    machine = {k: v.scores for k, v in store.by_scorer(right_name).items()}
    if not human or not machine:
        print(
            f"need labels from both scorers ({left_name}: {len(human)}, "
            f"{right_name}: {len(machine)})",
            file=sys.stderr,
        )
        return 2

    results = agreement.compare(human, machine)
    print(f"rubric  {RUBRIC_VERSION}")
    print(f"compare {left_name}  vs  {right_name}")
    print(f"floor   weighted kappa >= {agreement.GATING_FLOOR} to gate a build\n")
    header = f"  {'dimension':<22} {'n':>3}  {'raw':>5}  {'weighted':>8}  {'95% CI':>15}  verdict"
    print(header)
    for result in results:
        raw = f"{result.raw_agreement:.0%}"
        interval = agreement.bootstrap(human, machine, result.dimension)
        if interval is None:
            ci, risk = "—", None
        else:
            lo, hi, risk = interval
            ci = f"{lo:+.2f} to {hi:+.2f}"
        # The interval decides, not the point estimate alone: RC1-250 measured
        # 0.66 with 34% of the distribution below the floor, and the honest call
        # was not to gate. Encoding that beats re-litigating it every time.
        gates = result.gates and risk is not None and risk <= _MAX_RISK_OF_NOT_GATING
        verdict = "gates" if gates else "ADVISORY"
        print(
            f"  {result.dimension:<22} {result.n:>3}  {raw:>5}  {result.headline:>8}  "
            f"{ci:>15}  {verdict}"
        )
        if risk is not None:
            print(f"    {risk:.0%} of the interval is below the {agreement.GATING_FLOOR} floor")
        if result.note:
            print(f"    {result.note}")

    if args.verbose:
        for dimension in JUDGED_KEYS:
            table = agreement.confusion(human, machine, dimension)
            disagreements = {k: v for k, v in table.items() if k[0] != k[1]}
            if disagreements:
                print(f"\n  {dimension} — human -> judge, where they differed")
                for (h, j), count in sorted(disagreements.items(), key=lambda kv: -kv[1]):
                    print(f"    {h} -> {j}  (x{count})")

    advisory = [r.dimension for r in results if not r.gates]
    if advisory:
        print(f"\n  advisory (cannot fail a build in RC1-255): {', '.join(advisory)}")
    return 0


def _no_labels(store: LabelStore, scorer: str) -> str:
    """Explain an empty label set, distinguishing 'never scored' from 'scored
    under an older rubric'.

    `by_scorer` excludes labels from a superseded rubric on purpose — a 1 under
    different wording is not the same measurement. But reporting that as a bare
    "no labels" sends you off to re-label something you already labelled. The
    36 `human` labels from RC1-250 are exactly this case.
    """
    stale = sorted({label.rubric_version for label in store.all() if label.scorer == scorer})
    if not stale:
        known = sorted({label.scorer for label in store.all()})
        return f"no labels for scorer {scorer!r} (have: {', '.join(known) or 'none'})"
    return (
        f"no labels for scorer {scorer!r} under the current rubric {RUBRIC_VERSION!r} — "
        f"it has labels under {', '.join(stale)}, which are excluded rather than merged. "
        "Re-label under the current rubric, or calibrate a scorer from the same version."
    )


def cmd_construct(args: argparse.Namespace) -> int:
    """Does the judge detect degradation we planted ourselves? No human needed."""
    seeds = SeedStore(Path(args.seeds_path or SEEDS_PATH)).all()
    variants = {seed.id: seed.variant for seed in seeds}
    store = LabelStore(Path(args.labels_path or LABELS_PATH))
    scorer = args.scorer or judge.JUDGE_VERSION
    labels = {k: v.scores for k, v in store.by_scorer(scorer).items()}
    if not labels:
        print(_no_labels(store, scorer), file=sys.stderr)
        return 2

    print(f"scorer  {scorer}")
    print(f"check   does a '{construct.CLEAN}' output outrank a '{construct.PLANTED}' one?")
    print("        clean is grounded by construction; planted is degraded by construction")
    print("        only checked where the planted degradation targets the dimension\n")
    print(
        f"  {'dimension':<16} {'clean':>6} {'planted':>8} {'pairs':>6} {'ranked ok':>10}  verdict"
    )
    worst = 1.0
    for result in construct.separation(labels, variants):
        if not result.targeted:
            print(
                f"  {result.dimension:<16} {'—':>6} {'—':>8} {'—':>6} {'—':>10}  "
                "not targeted by the planted degradation"
            )
            continue
        verdict = "detects" if result.detects else "NO SIGNAL"
        print(
            f"  {result.dimension:<16} {result.clean_mean:>6.2f} {result.planted_mean:>8.2f} "
            f"{result.pairs:>6} {result.headline:>10}  {verdict}"
        )
        worst = min(worst, result.rate)

    print(
        "\n  Passing this earns no gating rights — it shows the judge is measuring "
        "something\n  real, not that it agrees with a careful human. See docs/judging.md."
    )
    return 0 if worst >= construct.SEPARATION_FLOOR else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals", description="Run and report quality evals for the planner's LLM systems."
    )
    parser.add_argument(
        "--runs-path",
        help="Override the run log location (default: LPA_EVALS_RUNS_PATH).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="Run a subject against its cases and record the result.")
    run_cmd.add_argument("subject", help=f"One of: {', '.join(sorted(SUBJECTS))}")
    run_cmd.add_argument(
        "-v", "--verbose", action="store_true", help="Show passing characteristics too."
    )
    run_cmd.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="Print a recorded run.")
    report.add_argument("run_id", nargs="?", help="Run id. Omit for the most recent run.")
    report.add_argument("--subject", help="With no run id, the latest run of this subject.")
    report.add_argument(
        "-q", "--quiet", action="store_true", help="Show only failing characteristics."
    )
    report.set_defaults(func=cmd_report)

    # --- judge calibration (RC1-250) ---------------------------------------
    parser.add_argument("--seeds-path", help="Override the seed set location.")
    parser.add_argument("--labels-path", help="Override the label store location.")

    seed = sub.add_parser("seed", help="Generate the calibration seed set (billed, run once).")
    seed.add_argument(
        "--force", action="store_true", help="Regenerate even if seeds already exist."
    )
    seed.set_defaults(func=cmd_seed)

    label = sub.add_parser("label", help="Score the seed set by hand. Free and resumable.")
    label.add_argument(
        "--dimension",
        help=(
            "Score ONE dimension across the whole set. Far more consistent than "
            "switching rubric on every item."
        ),
    )
    label.add_argument(
        "--scorer",
        default=labelling.HUMAN,
        help="Store under this scorer name. A second pass under a new name measures "
        "whether you agree with yourself.",
    )
    label.add_argument("--limit", type=int, help="Only offer the first N seeds.")
    label.set_defaults(func=cmd_label)

    judge_cmd = sub.add_parser("judge", help="Score the seed set with the judge (billed).")
    judge_cmd.add_argument(
        "--force", action="store_true", help="Re-score seeds this judge version already did."
    )
    judge_cmd.set_defaults(func=cmd_judge)

    calibrate = sub.add_parser("calibrate", help="Human-vs-judge agreement, per dimension.")
    calibrate.add_argument(
        "-v", "--verbose", action="store_true", help="Also show where they disagreed."
    )
    calibrate.add_argument(
        "--scorer", default=labelling.HUMAN, help="The left-hand scorer (default: human)."
    )
    calibrate.add_argument(
        "--against",
        help="The right-hand scorer (default: the judge). Pass a second human pass "
        "to measure whether you agree with yourself — the ceiling on any judge.",
    )
    calibrate.set_defaults(func=cmd_calibrate)

    con = sub.add_parser(
        "construct", help="Does a scorer detect the degradation we planted? No human needed."
    )
    con.add_argument("--scorer", help="Default: the judge.")
    con.set_defaults(func=cmd_construct)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
