"""The CLI, including the exit codes RC1-255 will gate on.

The codes are asserted here rather than left to the story that consumes them,
because a gate whose contract is only defined at the moment it is wired up is a
gate that gets wired up wrongly.
"""

from __future__ import annotations

from agent_evals.case import Case
from agent_evals.record import CaseResult, CharacteristicResult, RunStore, SubjectVersion, Usage
from evals.cli import main
from evals.subjects import SUBJECTS, health


def test_run_health_executes_scores_and_records(tmp_path, capsys):
    """The story's headline acceptance criterion."""
    runs = tmp_path / "runs.jsonl"
    exit_code = main(["--runs-path", str(runs), "run", "health"])
    out = capsys.readouterr().out

    assert exit_code == 0
    record = RunStore(runs).latest("health")
    assert record is not None
    assert len(record.results) == len(health.CASES) > 0
    assert record.passed == len(record.results)
    assert f"run {record.run_id} recorded" in out


def test_the_record_carries_version_and_cost_even_at_zero(tmp_path):
    """Acceptance criterion: version and cost are present when cost is zero."""
    runs = tmp_path / "runs.jsonl"
    main(["--runs-path", str(runs), "run", "health"])
    record = RunStore(runs).latest("health")

    assert record.subject_version.code_version
    assert record.subject_version.model is None
    assert record.total_cost_usd == 0
    assert record.total_latency_ms > 0


def test_report_prints_the_run_and_names_the_absent_model(tmp_path, capsys):
    runs = tmp_path / "runs.jsonl"
    main(["--runs-path", str(runs), "run", "health"])
    run_id = RunStore(runs).latest("health").run_id
    capsys.readouterr()

    exit_code = main(["--runs-path", str(runs), "report", run_id])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert run_id in out
    assert "none (deterministic)" in out
    assert "cost          $0" in out


def test_report_defaults_to_the_latest_run(tmp_path, capsys):
    runs = tmp_path / "runs.jsonl"
    main(["--runs-path", str(runs), "run", "health"])
    run_id = RunStore(runs).latest("health").run_id
    capsys.readouterr()

    main(["--runs-path", str(runs), "report"])
    assert run_id in capsys.readouterr().out


def test_report_on_an_empty_log_exits_2_and_says_where_it_looked(tmp_path, capsys):
    runs = tmp_path / "runs.jsonl"
    assert main(["--runs-path", str(runs), "report"]) == 2
    assert str(runs) in capsys.readouterr().err


def test_unknown_subject_exits_2_and_lists_the_known_ones(tmp_path, capsys):
    exit_code = main(["--runs-path", str(tmp_path / "runs.jsonl"), "run", "nonexistent"])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "health" in err


def test_quiet_report_hides_passing_characteristics(tmp_path, capsys):
    runs = tmp_path / "runs.jsonl"
    main(["--runs-path", str(runs), "run", "health"])
    capsys.readouterr()

    main(["--runs-path", str(runs), "report", "--quiet"])
    quiet = capsys.readouterr().out
    main(["--runs-path", str(runs), "report"])
    loud = capsys.readouterr().out

    assert "declares-server-version" in loud
    assert "declares-server-version" not in quiet
    # The header survives either way — a quiet report is still a report.
    assert "subject       health" in quiet


class _StubSubject:
    """A subject whose outcome is dictated by the test.

    Subjects are duck-typed modules (`NAME`, `CASES`, `version()`, `run()`), so a
    stub is an object with those four names and nothing else — which is also a
    working demonstration that the contract really is that small.
    """

    NAME = "stub"
    CASES = (Case(id="stub.only", expect=("whatever",)),)

    def __init__(self, result: CaseResult) -> None:
        self._result = result

    def version(self) -> SubjectVersion:
        return SubjectVersion(subject=self.NAME, code_version="0.0.0")

    def run(self, case, tmp_root) -> CaseResult:
        return self._result


def _register(monkeypatch, result: CaseResult) -> None:
    monkeypatch.setitem(SUBJECTS, "stub", _StubSubject(result))


def test_a_failing_case_exits_1(tmp_path, monkeypatch):
    """The RC1-255 gate contract: a quality regression is a non-zero exit."""
    _register(
        monkeypatch,
        CaseResult(
            case_id="stub.only",
            characteristics=[CharacteristicResult(name="whatever", passed=False, detail="nope")],
            usage=Usage(latency_ms=1.0),
        ),
    )
    assert main(["--runs-path", str(tmp_path / "runs.jsonl"), "run", "stub"]) == 1


def test_an_errored_case_exits_2_not_1(tmp_path, monkeypatch):
    """2 outranks 1 deliberately: "the subject is broken" and "the subject
    answered badly" need different people looking at them."""
    _register(
        monkeypatch,
        CaseResult(case_id="stub.only", usage=Usage(latency_ms=1.0), error="RuntimeError: boom"),
    )
    assert main(["--runs-path", str(tmp_path / "runs.jsonl"), "run", "stub"]) == 2


def test_report_replays_the_exit_code_of_a_recorded_run(tmp_path, monkeypatch):
    """A gate reading back a stored run must reach the same verdict as the run
    that produced it, or the trend view and the gate can disagree."""
    runs = tmp_path / "runs.jsonl"
    _register(
        monkeypatch,
        CaseResult(
            case_id="stub.only",
            characteristics=[CharacteristicResult(name="whatever", passed=False, detail="nope")],
            usage=Usage(latency_ms=1.0),
        ),
    )
    assert main(["--runs-path", str(runs), "run", "stub"]) == 1
    assert main(["--runs-path", str(runs), "report"]) == 1


def test_an_errored_case_is_reported_as_an_error_not_a_failure(tmp_path, monkeypatch, capsys):
    _register(
        monkeypatch,
        CaseResult(case_id="stub.only", usage=Usage(latency_ms=1.0), error="RuntimeError: boom"),
    )
    main(["--runs-path", str(tmp_path / "runs.jsonl"), "run", "stub"])
    out = capsys.readouterr().out
    assert "ERROR stub.only" in out
    assert "RuntimeError: boom" in out


def test_a_second_run_appends_rather_than_replacing(tmp_path):
    """Back-to-back runs are normal while iterating on a case file. At second
    precision the second run collided on its id and raised; see `new_run_id`."""
    runs = tmp_path / "runs.jsonl"
    main(["--runs-path", str(runs), "run", "health"])
    main(["--runs-path", str(runs), "run", "health"])

    records = RunStore(runs).all()
    assert len(records) == 2
    assert records[0].run_id != records[1].run_id


def test_stale_rubric_labels_are_explained_not_reported_as_absent(tmp_path):
    """A scorer with labels under a superseded rubric must not read as unlabelled.

    `by_scorer` excludes them deliberately, and the bare "no labels" it used to
    print sent you off to re-label 36 seeds that were already labelled.
    """
    from agent_evals.rubric import RUBRIC_VERSION
    from agent_evals.seeds import Label, LabelStore
    from evals.cli import _no_labels

    path = tmp_path / "labels.jsonl"
    store = LabelStore(path)
    store.append(
        Label(
            seed_id="s1",
            scorer="human",
            rubric_version="a-superseded-version",
            scores={"no-unsupported-claims": 2},
        )
    )

    message = _no_labels(store, "human")
    assert "a-superseded-version" in message, "must name the version the labels are under"
    assert RUBRIC_VERSION in message, "must name the version they would need to be under"

    absent = _no_labels(store, "never-scored-anything")
    assert "a-superseded-version" not in absent, "a genuinely unlabelled scorer is a different case"
    assert "human" in absent, "should say which scorers do exist"
