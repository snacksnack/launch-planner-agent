"""The walking skeleton: case → run → score, for a subject that costs nothing.

These do not re-assert `platform.health`'s behaviour — `apps/mcp/tests` already
does that, more cheaply and more directly. What is asserted here is the
*harness*: that a case's input selects a world, that characteristics are scored
individually, and that a broken characteristic fails loudly instead of skipping.
"""

from __future__ import annotations

import os

from evals.case import Case
from evals.subjects import health


def test_both_cases_score_every_characteristic_they_declare(tmp_path):
    for index, case in enumerate(health.CASES):
        result = health.run(case, tmp_path / f"case-{index}")
        assert result.error is None, result.error
        assert [c.name for c in result.characteristics] == list(case.expect)


def test_the_shipped_cases_pass(tmp_path):
    """If this fails, either `platform.health` regressed or a characteristic is
    wrong — the report's `detail` says which."""
    for index, case in enumerate(health.CASES):
        result = health.run(case, tmp_path / f"case-{index}")
        failures = [c for c in result.characteristics if not c.passed]
        assert not failures, [f"{c.name}: {c.detail}" for c in failures]


def test_case_input_selects_the_world(tmp_path):
    """`snapshots` is world state, not an argument — the tool takes none. The
    two shipped cases must therefore produce genuinely different output."""
    empty = health.run(health.CASES[0], tmp_path / "empty")
    populated = health.run(health.CASES[1], tmp_path / "populated")

    absent = next(c for c in empty.characteristics if c.name.startswith("reports-store-absent"))
    counted = next(c for c in populated.characteristics if c.name == "reports-snapshot-count")
    assert absent.passed and counted.passed
    assert "1 snapshot(s)" in counted.detail


def test_an_unknown_characteristic_fails_rather_than_skipping(tmp_path):
    """A typo in a case file must not read as a pass."""
    case = Case(id="typo", input={"snapshots": 0}, expect=("reprots-all-components",))
    result = health.run(case, tmp_path / "typo")
    assert result.passed is False
    assert result.characteristics[0].detail == "no predicate is registered for this name"


def test_a_failing_characteristic_carries_an_actionable_detail(tmp_path):
    """`detail` is what a reader acts on, so a failure must name what it saw."""
    case = Case(id="wrong-count", input={"snapshots": 0}, expect=("reports-snapshot-count",))
    result = health.run(case, tmp_path / "wrong-count")
    characteristic = result.characteristics[0]
    assert characteristic.passed is False
    assert "expected 0 snapshot(s)" in characteristic.detail


def test_version_declares_no_model_for_a_deterministic_subject(tmp_path):
    version = health.version()
    assert version.subject == "health"
    assert version.code_version
    assert version.model is None
    assert version.prompt_version is None


def test_usage_is_zero_cost_but_real_latency(tmp_path):
    result = health.run(health.CASES[0], tmp_path / "usage")
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.cost_usd == 0
    assert result.usage.latency_ms > 0


def test_the_run_leaves_the_developers_own_store_alone(tmp_path, monkeypatch):
    """The isolation contract: after a run, the environment is exactly as it was.
    A harness that leaked `LPA_DATABASE_URL` would silently repoint the CLI."""
    monkeypatch.setenv("LPA_DATABASE_URL", "sqlite:///./sentinel.db")
    health.run(health.CASES[0], tmp_path / "isolation")
    assert os.environ["LPA_DATABASE_URL"] == "sqlite:///./sentinel.db"
