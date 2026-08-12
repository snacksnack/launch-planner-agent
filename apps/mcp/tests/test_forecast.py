"""`plan.forecast`.

The engine is trusted (ADR-0022, ADR-0026). What matters here is that the
numbers survive the wrapping unchanged, that a run can be reproduced, and that
the plan's own optimistic date is never handed over as if it were the answer.
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
from mcp.client.client import Client
from mcp_server.server import build_server
from planner_core import Plan, monte_carlo

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = REPO_ROOT / "fixtures/jira-cloud-migration/golden/expected-plan.json"
START = "2026-08-03"


def _call(args: dict | None = None) -> dict:
    result = asyncio.run(
        build_server().call_tool("plan.forecast", {"start": START, **(args or {})})
    )
    assert result.is_error is False, result.content
    return result.structured_content


def _as_a_client(args: dict):
    async def run():
        async with Client(build_server()) as client:
            return await client.call_tool("plan.forecast", {"start": START, **args})

    return asyncio.run(run())


def _tool_schema() -> dict:
    async def run():
        async with Client(build_server()) as client:
            return await client.list_tools()

    tools = asyncio.run(run()).tools
    return next(t for t in tools if t.name == "plan.forecast").model_dump()


# --- parity with the engine and the CLI -------------------------------------


def test_the_band_matches_the_engine_for_the_same_seed():
    expected = monte_carlo(
        Plan.model_validate_json(GOLDEN.read_text()),
        start_date=date.fromisoformat(START),
        iterations=1000,
        seed=7,
    )
    payload = _call({"seed": 7})

    assert payload["p50"] == expected.p50.isoformat()
    assert payload["p80"] == expected.p80.isoformat()
    assert payload["p90"] == expected.p90.isoformat()
    assert payload["deterministic_date"] == expected.deterministic_finish.isoformat()
    assert payload["mean_working_days"] == expected.mean_working_days


def test_the_band_matches_the_cli_for_the_same_seed():
    """The acceptance criterion: verified in a test, not by eye."""
    payload = _call({"seed": 42})
    completed = subprocess.run(
        [
            str(Path(sys.executable).parent / "plan"),
            "forecast",
            str(GOLDEN),
            "--start-date",
            START,
            "--seed",
            "42",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    band = re.search(
        r"P50\s+(\d{4}-\d{2}-\d{2})\s+P80\s+(\d{4}-\d{2}-\d{2})\s+P90\s+(\d{4}-\d{2}-\d{2})",
        completed.stdout,
    )
    assert band, f"unexpected CLI output:\n{completed.stdout}"
    assert payload["p50"] == band.group(1)
    assert payload["p80"] == band.group(2)
    assert payload["p90"] == band.group(3)

    point = re.search(r"Point estimate \(likely durations\): (\d{4}-\d{2}-\d{2})", completed.stdout)
    assert point and payload["deterministic_date"] == point.group(1)


# --- reproducibility --------------------------------------------------------


def test_the_seed_is_echoed_even_when_it_defaulted():
    """A forecast nobody can reproduce is a number nobody can defend."""
    payload = _call()
    assert payload["seed"] == 0
    assert payload["iterations"] == 1000


def test_the_same_seed_reproduces_the_run_exactly():
    first, second = _call({"seed": 99}), _call({"seed": 99})
    for field in ("p50", "p80", "p90", "mean_working_days", "deterministic_confidence"):
        assert first[field] == second[field]
    assert first["criticality"] == second["criticality"]


def test_a_different_seed_gives_a_different_sample():
    assert _call({"seed": 1})["mean_working_days"] != _call({"seed": 2})["mean_working_days"]


# --- the optimism gap -------------------------------------------------------


def test_the_deterministic_date_is_reported_separately_from_p50():
    """Handed both figures unlabelled, a model reports the earlier one."""
    payload = _call()
    assert payload["deterministic_date"] != payload["p50"]
    assert date.fromisoformat(payload["deterministic_date"]) < date.fromisoformat(
        payload["p50"]
    )


def test_the_deterministic_date_carries_its_actual_confidence():
    """On the golden the plan says Oct 12 and fewer than a quarter of runs get
    there. Returning the probability beats labelling the date 'optimistic'."""
    payload = _call()
    assert 0.0 < payload["deterministic_confidence"] < 0.5


def test_confidence_matches_an_independent_count_of_the_runs():
    result = monte_carlo(
        Plan.model_validate_json(GOLDEN.read_text()),
        start_date=date.fromisoformat(START),
        iterations=1000,
        seed=5,
    )
    target = result.deterministic_finish.isoformat()
    hits = sum(b["count"] for b in result.distribution if b["date"] <= target)

    assert _call({"seed": 5})["deterministic_confidence"] == round(hits / 1000, 4)


def test_the_summary_leads_with_the_band_not_the_plan_date():
    summary = _call()["summary"]
    assert summary.startswith("80% chance")
    assert "optimistic" in summary
    assert "rather than as the date" in summary


def test_the_band_is_ordered():
    payload = _call()
    assert (
        date.fromisoformat(payload["p50"])
        <= date.fromisoformat(payload["p80"])
        <= date.fromisoformat(payload["p90"])
    )


# --- criticality index ------------------------------------------------------


def test_criticality_is_sorted_most_critical_first():
    values = [entry["criticality"] for entry in _call()["criticality"]]
    assert values == sorted(values, reverse=True)


def test_only_tasks_that_were_ever_critical_are_returned():
    payload = _call({"top_tasks": 50})
    assert all(entry["criticality"] > 0 for entry in payload["criticality"])
    assert payload["tasks_ever_critical"] == len(payload["criticality"])
    assert payload["criticality_truncated"] is False


def test_top_tasks_truncates_and_says_so():
    payload = _call({"top_tasks": 3})
    assert len(payload["criticality"]) == 3
    assert payload["criticality_truncated"] is True
    assert payload["tasks_ever_critical"] > 3


def test_criticality_entries_name_an_owner():
    """'Who should I talk to' is the follow-up question to 'what is risky'."""
    assert any(entry["owner_name"] for entry in _call()["criticality"])


def test_the_most_critical_task_is_on_the_deterministic_critical_path():
    """A sanity check tying the two tools together: the task critical in every
    run had better be critical in the single deterministic pass too."""
    forecast = _call()
    result = asyncio.run(
        build_server().call_tool("plan.critical_path", {"start": START})
    ).structured_content
    critical_ids = {t for chain in result["chains"] for t in chain["task_ids"]}
    assert forecast["criticality"][0]["task_id"] in critical_ids


# --- correlation is deliberately not a knob ---------------------------------


def test_correlation_is_not_exposed_as_a_parameter():
    """ADR-0026 kept it out of the UI because the units are unestimable. A model
    is likelier than a human to set a plausible-sounding value, and the failure
    is silent — numbers that match nothing on the dashboard."""
    properties = _tool_schema()["input_schema"]["properties"]
    assert "correlation" not in properties


def test_correlation_is_still_echoed_so_a_stored_forecast_records_how_it_ran():
    assert _call()["correlation"] == 0.0


# --- bounds -----------------------------------------------------------------


@pytest.mark.parametrize("iterations", [99, 5001, 0, -1])
def test_iterations_outside_the_api_bounds_are_rejected(iterations):
    result = _as_a_client({"iterations": iterations})
    assert result.is_error is True
    assert "[invalid_argument]" in result.content[0].text


@pytest.mark.parametrize("iterations", [100, 5000])
def test_the_boundary_values_are_accepted(iterations):
    assert _call({"iterations": iterations})["iterations"] == iterations


@pytest.mark.parametrize("top_tasks", [0, -1, 51])
def test_top_tasks_bounds_are_enforced(top_tasks):
    result = _as_a_client({"top_tasks": top_tasks})
    assert result.is_error is True
    assert "[invalid_argument]" in result.content[0].text


# --- shape ------------------------------------------------------------------


def test_the_response_size_is_bounded_regardless_of_iterations():
    """The finish-date histogram is a chart, not an answer. Without excluding it
    the payload would grow with the spread of the distribution."""
    small = len(json.dumps(_call({"iterations": 100})))
    large = len(json.dumps(_call({"iterations": 5000})))
    assert small < 4_000
    assert large < 4_000
    assert abs(large - small) < 200


def test_the_histogram_is_not_returned():
    blob = json.dumps(_call())
    assert "distribution" not in blob


def test_the_response_carries_provenance():
    payload = _call()
    assert payload["ref"]["canonical_ref"]
    assert payload["ref"]["content_hash"]
    assert payload["start_date"] == START
    assert payload["computed_at"]


def test_the_description_separates_this_from_plan_critical_path():
    description = _tool_schema()["description"]
    assert "plan.critical_path" in description
    assert "criticality index" in description
    assert "biased early" in description
