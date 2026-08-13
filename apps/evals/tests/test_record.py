"""Run records and the append-only log.

The load-bearing test here is `test_appending_leaves_earlier_lines_untouched`.
`RunStore` cannot enforce immutability the way the plan store does (ADR-0012
installs SQLite triggers; a text file has none), so the guarantee is structural
and this is what holds it honest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from evals.record import (
    CaseResult,
    CharacteristicResult,
    DuplicateRunId,
    RunRecord,
    RunStore,
    SubjectVersion,
    Usage,
    load_jsonl,
    new_run_id,
)

_WHEN = datetime(2026, 8, 13, 17, 42, 33, tzinfo=UTC)


def _record(run_id: str = "health-20260813T174233.000000Z", *, failing: bool = False) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        subject_version=SubjectVersion(subject="health", code_version="0.1.0"),
        started_at=_WHEN,
        finished_at=_WHEN + timedelta(seconds=1),
        results=[
            CaseResult(
                case_id="c1",
                characteristics=[
                    CharacteristicResult(name="a", passed=not failing, detail="because")
                ],
                usage=Usage(latency_ms=12.5),
            )
        ],
    )


def test_run_id_is_chronologically_sortable():
    """Lexical order is chronological order, so "the newest run" is a sort
    rather than a parse — the same property backup keys have (ADR-0029)."""
    earlier = new_run_id("health", _WHEN)
    later = new_run_id("health", _WHEN + timedelta(seconds=1))
    assert earlier == "health-20260813T174233.000000Z"
    assert earlier < later


def test_run_id_normalises_to_utc():
    """A run stamped in local time would sort wrongly against one stamped in UTC."""
    two_hours_ahead = timezone(timedelta(hours=2))
    assert new_run_id("health", _WHEN.astimezone(two_hours_ahead)) == new_run_id("health", _WHEN)


def test_append_and_read_back(tmp_path):
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record())
    assert store.get("health-20260813T174233.000000Z").subject_version.subject == "health"
    assert store.get("missing") is None


def test_append_creates_the_parent_directory(tmp_path):
    store = RunStore(tmp_path / "nested" / "deeper" / "runs.jsonl")
    store.append(_record())
    assert store.path.exists()


def test_appending_leaves_earlier_lines_untouched(tmp_path):
    """The append-only guarantee, asserted at the byte level."""
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record("health-20260813T174233.000000Z"))
    first_line = store.path.read_text().splitlines()[0]

    store.append(_record("health-20260813T174234Z"))

    lines = store.path.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] == first_line


def test_duplicate_run_ids_are_rejected(tmp_path):
    """Two records under one id would make `evals report <id>` silently pick one."""
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record())
    with pytest.raises(DuplicateRunId):
        store.append(_record())


def test_a_malformed_line_is_a_hard_error(tmp_path):
    """Skipping an unreadable record would understate a trend rather than
    reporting that the log is damaged."""
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record())
    store.path.write_text(store.path.read_text() + "{not json}\n")
    with pytest.raises(ValueError, match="not a valid run record"):
        store.all()


def test_missing_log_reads_as_empty_rather_than_raising(tmp_path):
    assert RunStore(tmp_path / "absent.jsonl").all() == []


def test_latest_filters_by_subject(tmp_path):
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record("health-20260813T174233.000000Z"))
    other = _record("other-20260813T174299.000000Z")
    other.subject_version.subject = "other"
    store.append(other)

    assert store.latest().run_id == "other-20260813T174299.000000Z"
    assert store.latest("health").run_id == "health-20260813T174233.000000Z"
    assert store.latest("nobody") is None


def test_totals_count_passes_failures_and_errors():
    record = _record(failing=True)
    record.results.append(
        CaseResult(case_id="c2", usage=Usage(latency_ms=1.0), error="RuntimeError: boom")
    )
    assert record.passed == 0
    assert record.failed == 2
    assert record.errored == 1


def test_an_errored_case_never_counts_as_passed():
    """An outage must not read as a quality result — the distinction `error`
    exists to preserve."""
    result = CaseResult(case_id="c1", characteristics=[], usage=Usage(latency_ms=0.0), error="down")
    assert result.passed is False


def test_zero_cost_is_recorded_not_omitted(tmp_path):
    """Acceptance criterion: run records carry cost even when it is zero."""
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record())
    raw = load_jsonl(store.path)[0]
    assert raw["results"][0]["usage"]["cost_usd"] == "0"
    assert raw["results"][0]["usage"]["input_tokens"] == 0


def test_cost_stays_exact_across_the_json_round_trip(tmp_path):
    """Decimal, not float: per-case costs are fractions of a cent and get summed
    across hundreds of cases."""
    store = RunStore(tmp_path / "runs.jsonl")
    record = _record()
    record.results[0].usage.cost_usd = Decimal("0.0000031")
    store.append(record)
    assert store.get(record.run_id).results[0].usage.cost_usd == Decimal("0.0000031")


def test_subject_version_states_the_absence_of_a_model(tmp_path):
    """For a deterministic subject the honest value is "there is no model", and
    a missing key would read as "we forgot to record it"."""
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(_record())
    raw = load_jsonl(store.path)[0]
    assert raw["subject_version"]["model"] is None
    assert raw["subject_version"]["prompt_version"] is None
    assert raw["subject_version"]["code_version"]
