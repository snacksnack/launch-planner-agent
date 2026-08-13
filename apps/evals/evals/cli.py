"""`evals` CLI — run a subject against its cases, and read a run back.

    uv run evals run health
    uv run evals report health-20260813T174233Z

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

from evals.config import get_eval_settings
from evals.record import RunRecord, RunStore, new_run_id
from evals.subjects import SUBJECTS

_PASS = "pass"
_FAIL = "FAIL"


def _store(args: argparse.Namespace) -> RunStore:
    return RunStore(Path(args.runs_path) if args.runs_path else get_eval_settings().runs_path)


def cmd_run(args: argparse.Namespace) -> int:
    subject = SUBJECTS.get(args.subject)
    if subject is None:
        known = ", ".join(sorted(SUBJECTS)) or "(none registered)"
        print(f"unknown subject {args.subject!r}. Known subjects: {known}", file=sys.stderr)
        return 2

    started_at = datetime.now(UTC)
    results = []
    # One scratch root for the whole run so a case cannot inherit the previous
    # case's store — cross-case leakage is the classic way a suite goes green
    # for the wrong reason.
    with TemporaryDirectory(prefix="evals-") as tmp:
        for index, case in enumerate(subject.CASES):
            case_root = Path(tmp) / f"{index:03d}-{case.id}"
            case_root.mkdir(parents=True)
            results.append(subject.run(case, case_root))

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
        print(f"no record found for {which} in {store.path}", file=sys.stderr)
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
            mark = "✓" if characteristic.passed else "✗"
            print(f"    {mark} {characteristic.name}: {characteristic.detail}")


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
