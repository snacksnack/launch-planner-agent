"""`plan.simulate`.

The engine is already trusted (ADR-0015). What this file guards is the wrapping,
and specifically the two ways this tool could return a confident wrong answer:
an unresolved task reference reaching an engine that skips it silently, and a
zero launch shift that means "absorbed" being read as "nothing happened" or vice
versa.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from app.config import get_settings
from app.store import SQLiteEventStore
from mcp.client.client import Client
from mcp_server.errors import AmbiguousTaskRef, TaskNotFound
from mcp_server.server import build_server
from mcp_server.tools import simulate as simulate_module
from mcp_server.tools.simulate import resolve_task_ref
from planner_core import Plan

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = REPO_ROOT / "fixtures/jira-cloud-migration/golden/expected-plan.json"
START = "2026-08-03"


def _plan() -> Plan:
    return Plan.model_validate_json(GOLDEN.read_text())


def _call(args: dict) -> dict:
    result = asyncio.run(build_server().call_tool("plan.simulate", {"start": START, **args}))
    assert result.is_error is False, result.content
    return result.structured_content


def _as_a_client(args: dict):
    async def run():
        async with Client(build_server()) as client:
            return await client.call_tool("plan.simulate", {"start": START, **args})

    return asyncio.run(run())


# --- task resolution --------------------------------------------------------


def test_an_exact_id_wins():
    resolved = resolve_task_ref(_plan(), "task-legal-review")
    assert resolved.id == "task-legal-review"
    assert resolved.matched_on == "id"


def test_a_full_name_resolves():
    resolved = resolve_task_ref(_plan(), "Obtain legal sign-off for client data migration")
    assert resolved.id == "task-legal-review"
    assert resolved.matched_on == "name"


def test_the_id_is_searchable_as_a_phrase():
    """Ids are short slugs while names are full sentences, so "legal review" is
    the id spoken aloud. If only names were searched, the most natural reference
    a person uses would miss entirely."""
    resolved = resolve_task_ref(_plan(), "legal review")
    assert resolved.id == "task-legal-review"
    assert resolved.matched_on == "task key"


def test_resolution_is_case_insensitive():
    assert resolve_task_ref(_plan(), "LEGAL REVIEW").id == "task-legal-review"
    assert resolve_task_ref(_plan(), "Task-Legal-Review").id == "task-legal-review"


def test_a_unique_substring_resolves():
    assert resolve_task_ref(_plan(), "sign-off for client").id == "task-legal-review"


def test_an_ambiguous_reference_returns_candidates_rather_than_guessing():
    with pytest.raises(AmbiguousTaskRef) as caught:
        resolve_task_ref(_plan(), "migration")
    assert len(caught.value.candidates) > 1
    assert "matches" in str(caught.value)


def test_an_unmatched_reference_suggests_near_misses():
    with pytest.raises(TaskNotFound) as caught:
        resolve_task_ref(_plan(), "legl reviw")
    assert "Did you mean" in str(caught.value)


def test_an_unmatched_reference_with_no_near_miss_still_says_where_to_look():
    with pytest.raises(TaskNotFound) as caught:
        resolve_task_ref(_plan(), "zzzzzzzz")
    assert "plan.critical_path" in str(caught.value)


def test_an_empty_reference_is_rejected():
    result = _as_a_client({"task": "   ", "days": 5})
    assert result.is_error is True
    assert "[invalid_argument]" in result.content[0].text


# --- the silent no-op: the engine must never see an unresolved id -----------


def test_an_unresolved_reference_never_reaches_the_engine(monkeypatch):
    """apply_scenario skips an unknown id with a warning and still returns a
    schedule. Passing one through would produce a successful response with an
    unchanged date, which reads as 'no impact on the launch'."""
    called = False

    def spy(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("simulate() was reached with an unresolved task")

    monkeypatch.setattr(simulate_module, "simulate", spy)

    result = _as_a_client({"task": "no such task at all", "days": 5})
    assert result.is_error is True
    assert "[task_not_found]" in result.content[0].text
    assert called is False


def test_an_ambiguous_reference_never_reaches_the_engine(monkeypatch):
    monkeypatch.setattr(
        simulate_module,
        "simulate",
        lambda *a, **kw: pytest.fail("simulate() was reached with an ambiguous task"),
    )
    result = _as_a_client({"task": "migration", "days": 5})
    assert result.is_error is True
    assert "[ambiguous_task_ref]" in result.content[0].text


# --- absorbed vs not applied: identical deltas, opposite meanings -----------


def test_a_slip_inside_float_is_reported_as_absorbed():
    payload = _call({"task": "legal review", "days": 2})
    assert payload["outcome"] == "absorbed_by_float"
    assert payload["applied"] is True
    assert payload["launch_shift_working_days"] == 0
    assert payload["float_before_working_days"] == 6.0
    assert "absorbed" in payload["summary"]
    assert payload["warnings"] == []


def test_a_slip_beyond_float_moves_the_launch_by_the_difference():
    """Textbook float behaviour, and the arithmetic a reader can check: 30 days
    of slip against 6 days of float moves the date by 24."""
    payload = _call({"task": "legal review", "days": 30})
    assert payload["outcome"] == "launch_moved"
    assert payload["launch_shift_working_days"] == 24
    assert payload["launch_before"] == "2026-10-12"
    assert payload["launch_after"] == "2026-11-13"


def test_a_critical_task_moves_the_launch_by_the_full_slip():
    payload = _call({"task": "task-bulk-migration", "days": 5})
    assert payload["float_before_working_days"] == 0.0
    assert payload["launch_shift_working_days"] == 5


def test_warnings_force_the_not_applied_outcome(monkeypatch):
    """The only signal that the engine rejected a change. A result with warnings
    must never be presented as clean, even though its delta is all zeros."""
    real = simulate_module.simulate

    def with_warnings(*args, **kwargs):
        result = real(*args, **kwargs)
        result.warnings.append("delay_task: something was skipped")
        return result

    monkeypatch.setattr(simulate_module, "simulate", with_warnings)

    payload = _call({"task": "legal review", "days": 2})
    assert payload["outcome"] == "not_applied"
    assert payload["applied"] is False
    assert payload["warnings"]
    assert "Nothing was simulated" in payload["summary"]
    assert "not because the plan absorbed it" in payload["summary"]


def test_the_two_zero_shift_outcomes_are_distinguishable(monkeypatch):
    """Both leave the launch date unmoved. If a caller could not tell them
    apart, the tool would be reporting a failure as a finding."""
    absorbed = _call({"task": "legal review", "days": 2})

    real = simulate_module.simulate
    monkeypatch.setattr(
        simulate_module,
        "simulate",
        lambda *a, **kw: _with_warning(real(*a, **kw)),
    )
    rejected = _call({"task": "legal review", "days": 2})

    assert absorbed["launch_shift_working_days"] == rejected["launch_shift_working_days"] == 0
    assert absorbed["outcome"] != rejected["outcome"]
    assert absorbed["applied"] is not rejected["applied"]


def _with_warning(result):
    result.warnings.append("delay_task: rejected")
    return result


# --- critical-path movement -------------------------------------------------


def test_critical_path_changes_are_reported_when_the_slip_reroutes():
    payload = _call({"task": "legal review", "days": 30})
    assert payload["critical_path_changed"] is True
    assert "Obtain legal sign-off for client data migration" in payload["critical_joined"]
    assert payload["critical_left"]


def test_no_critical_change_is_reported_for_an_absorbed_slip():
    payload = _call({"task": "legal review", "days": 2})
    assert payload["critical_path_changed"] is False
    assert payload["critical_joined"] == []


# --- parity with the CLI ----------------------------------------------------


def _run_cli(slip: str) -> subprocess.CompletedProcess:
    """`plan simulate` for one slip.

    Deliberately not `check=True`: `cmd_simulate` returns 1 when the launch
    moves and 0 when it does not, so the exit status is a *signal* rather than a
    failure. Treating it as a crash would make this test fail on precisely the
    scenario it exists to verify.
    """
    return subprocess.run(
        [
            str(Path(sys.executable).parent / "plan"),
            "simulate",
            str(GOLDEN),
            "--start-date",
            START,
            "--slip",
            slip,
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_the_delta_matches_the_cli_for_a_slipping_scenario():
    """The literal acceptance criterion. Parity is structural because both call
    planner_core in process, but argument handling and defaults can still drift,
    and that is exactly what this catches."""
    payload = _call({"task": "task-legal-review", "days": 30})
    completed = _run_cli("task-legal-review:30")

    assert completed.returncode == 1, completed.stderr
    headline = completed.stdout.splitlines()[0]
    match = re.search(
        r"(\d+) working day\(s\): (\d{4}-\d{2}-\d{2}) → (\d{4}-\d{2}-\d{2})", headline
    )
    assert match, f"unexpected CLI headline: {headline!r}"

    assert payload["launch_shift_working_days"] == int(match.group(1))
    assert payload["launch_before"] == match.group(2)
    assert payload["launch_after"] == match.group(3)
    assert payload["outcome"] == "launch_moved"


def test_the_cli_and_the_tool_agree_that_a_slip_was_absorbed():
    """Both surfaces have to reach the same verdict on the case that is easy to
    get wrong. The CLI says so in prose and in its exit code; the tool says so
    in `outcome`. If those ever disagree, one of them is lying about float."""
    payload = _call({"task": "task-legal-review", "days": 2})
    completed = _run_cli("task-legal-review:2")

    assert completed.returncode == 0, completed.stderr
    headline = completed.stdout.splitlines()[0]
    assert "No impact on the projected launch date" in headline
    assert "absorbed by available float" in headline

    assert payload["outcome"] == "absorbed_by_float"
    assert payload["launch_shift_working_days"] == 0
    assert payload["launch_after"] in headline


# --- nothing is persisted ---------------------------------------------------


def test_no_scenario_is_persisted_by_any_call():
    """`simulate()` applies the scenario to a deep copy. Saving a named scenario
    is a write and lives only on the CLI — this asserts the tool never drifts
    into it."""
    for days in (2, 30):
        _call({"task": "legal review", "days": days})

    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        assert store.list_scenarios() == []
        assert store.history() == []
    finally:
        store.close()


def test_the_underlying_plan_is_not_mutated():
    before = GOLDEN.read_text()
    _call({"task": "legal review", "days": 30})
    assert GOLDEN.read_text() == before


# --- argument validation ----------------------------------------------------


@pytest.mark.parametrize("days", [0, -5, 251])
def test_an_out_of_range_slip_is_rejected(days):
    result = _as_a_client({"task": "legal review", "days": days})
    assert result.is_error is True
    assert "[invalid_argument]" in result.content[0].text


def test_slip_is_documented_as_working_days():
    """A model that assumes calendar days is wrong by every weekend in the slip."""

    async def run():
        async with Client(build_server()) as client:
            return await client.list_tools()

    tools = asyncio.run(run()).tools
    description = next(t for t in tools if t.name == "plan.simulate").description
    assert "WORKING days" in description
    assert "absorbed_by_float" in description
    assert "not_applied" in description


# --- shape ------------------------------------------------------------------


def test_the_response_carries_provenance_and_the_resolved_task():
    payload = _call({"task": "legal review", "days": 5})
    assert payload["ref"]["canonical_ref"]
    assert payload["task"]["id"] == "task-legal-review"
    assert payload["task"]["name"]
    assert payload["slip_working_days"] == 5
    assert payload["computed_at"]


def test_the_response_stays_bounded_even_on_a_plan_wide_reschedule():
    payload = _call({"task": "task-plugin-audit", "days": 40})
    assert len(json.dumps(payload)) < 16_000
    assert payload["moved_task_count"] >= len(payload["moved_tasks"])
    if payload["moved_tasks_truncated"]:
        assert len(payload["moved_tasks"]) == 25


def test_launch_dates_are_real_dates():
    payload = _call({"task": "legal review", "days": 30})
    assert date.fromisoformat(payload["launch_before"]) < date.fromisoformat(
        payload["launch_after"]
    )
