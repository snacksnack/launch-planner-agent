"""`plan.critical_path`.

The correctness risk this tool carries is not arithmetic — the engine already
computed everything — it is *selection*: dropping a chain, or re-ordering one,
turns a correct schedule into a confident wrong answer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

from app.gantt import build_gantt_payload
from mcp.client.client import Client
from mcp_server.server import build_server
from planner_core import Plan, schedule_plan

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "fixtures/jira-cloud-migration/golden/expected-plan.json"
)
START = date(2026, 8, 3)


def _call(args: dict | None = None) -> dict:
    result = asyncio.run(build_server().call_tool("plan.critical_path", args or {}))
    assert result.is_error is False, result.content
    return result.structured_content


def _as_a_client(args: dict):
    async def run():
        async with Client(build_server()) as client:
            return await client.call_tool("plan.critical_path", args)

    return asyncio.run(run())


def _engine():
    """What the UI renders for the same plan and start date."""
    plan = Plan.model_validate_json(GOLDEN.read_text())
    schedule = schedule_plan(plan, start_date=START)
    return plan, schedule, build_gantt_payload(plan, schedule)


# --- parity with the engine the UI uses -------------------------------------


def test_chains_match_the_engine_exactly():
    _, schedule, _ = _engine()
    payload = _call({"start": START.isoformat()})
    assert [c["task_ids"] for c in payload["chains"]] == schedule.critical_chains


def test_float_values_match_the_gantt_payload():
    _, _, gantt = _engine()
    by_id = {t["id"]: t for t in gantt["tasks"]}
    for chain in _call({"start": START.isoformat()})["chains"]:
        for task in chain["tasks"]:
            expected = by_id[task["id"]]
            assert task["total_float"] == expected["total_float"]
            assert task["free_float"] == expected["free_float"]
            assert task["is_critical"] is expected["is_critical"]
            assert task["start"] == expected["start"]
            assert task["finish"] == expected["end"]


def test_launch_date_and_deadlines_match_the_engine():
    _, schedule, _ = _engine()
    payload = _call({"start": START.isoformat()})
    assert payload["launch_date"] == schedule.project_finish_date.isoformat()
    assert payload["duration_working_days"] == schedule.project_duration
    assert payload["meets_all_deadlines"] is schedule.meets_all_deadlines
    assert len(payload["deadlines"]) == len(schedule.deadline_checks)


# --- the plural-chains hazard -----------------------------------------------


def test_the_golden_has_more_than_one_chain_and_all_are_reported():
    """The flagship plan converges, so reporting a single path would drop a real
    critical chain on the demo plan itself."""
    payload = _call({"start": START.isoformat()})
    assert payload["chain_count"] >= 2
    assert len(payload["chains"]) == payload["chain_count"]


def test_the_chains_are_genuinely_different():
    chains = [tuple(c["task_ids"]) for c in _call({"start": START.isoformat()})["chains"]]
    assert len(set(chains)) == len(chains)


def test_critical_task_count_is_distinct_tasks_not_the_sum_of_chain_lengths():
    """Converging chains share tasks. Summing lengths would overstate how much
    work is actually critical — 19 rather than 11 on the golden."""
    payload = _call({"start": START.isoformat()})
    distinct = {t for c in payload["chains"] for t in c["task_ids"]}
    assert payload["critical_task_count"] == len(distinct)
    assert sum(c["length"] for c in payload["chains"]) > len(distinct)


def test_every_chain_task_is_actually_critical():
    _, schedule, _ = _engine()
    critical = set(schedule.critical_path_ids)
    for chain in _call({"start": START.isoformat()})["chains"]:
        assert set(chain["task_ids"]) <= critical


# --- order is meaning -------------------------------------------------------


def test_each_chain_is_ordered_by_dependency_not_by_id():
    """Every consecutive pair must be a real precedence edge. Sorting by id
    would leave a plausible-looking list that describes nothing."""
    plan, _, _ = _engine()
    edges = {(d.predecessor_id, d.successor_id) for d in plan.dependencies}

    chains = _call({"start": START.isoformat()})["chains"]
    for chain in chains:
        ids = chain["task_ids"]
        for predecessor, successor in zip(ids, ids[1:], strict=False):
            assert (predecessor, successor) in edges, f"{predecessor} -> {successor} is not an edge"

    assert any(c["task_ids"] != sorted(c["task_ids"]) for c in chains), (
        "the golden's chains happen to be alphabetical; this test proves nothing"
    )


def test_tasks_are_in_the_same_order_as_task_ids():
    for chain in _call({"start": START.isoformat()})["chains"]:
        assert [t["id"] for t in chain["tasks"]] == chain["task_ids"]


def test_chain_dates_run_forward():
    for chain in _call({"start": START.isoformat()})["chains"]:
        finishes = [t["finish"] for t in chain["tasks"]]
        assert finishes == sorted(finishes)


# --- owners -----------------------------------------------------------------


def test_owners_are_distinct_and_in_chain_order():
    chain = _call({"start": START.isoformat()})["chains"][0]
    owners = chain["owners"]
    assert len(owners) == len(set(owners))
    first_seen = list(
        dict.fromkeys(t["owner_name"] for t in chain["tasks"] if t["owner_name"])
    )
    assert owners == first_seen


# --- near-critical ----------------------------------------------------------


def test_near_critical_is_absent_unless_requested():
    payload = _call({"start": START.isoformat()})
    assert payload["near_critical"] is None
    assert payload["near_critical_threshold"] is None


def test_near_critical_honours_the_threshold():
    _, _, gantt = _engine()
    for threshold in (1.0, 2.0, 10.0):
        payload = _call(
            {
                "start": START.isoformat(),
                "include_near_critical": True,
                "near_critical_threshold": threshold,
            }
        )
        expected = {
            t["id"]
            for t in gantt["tasks"]
            if not t["is_critical"] and 0 < t["total_float"] <= threshold
        }
        assert {t["id"] for t in payload["near_critical"]} == expected
        assert payload["near_critical_threshold"] == threshold


def test_a_wider_threshold_never_returns_fewer_tasks():
    def count(threshold: float) -> int:
        return len(
            _call(
                {
                    "start": START.isoformat(),
                    "include_near_critical": True,
                    "near_critical_threshold": threshold,
                }
            )["near_critical"]
        )

    assert count(1.0) <= count(2.0) <= count(10.0)


def test_near_critical_excludes_tasks_already_on_the_critical_path():
    """Zero-float tasks are critical and already in `chains`; including them
    here would double-count the same risk."""
    payload = _call(
        {
            "start": START.isoformat(),
            "include_near_critical": True,
            "near_critical_threshold": 10.0,
        }
    )
    critical = {t for c in payload["chains"] for t in c["task_ids"]}
    for task in payload["near_critical"]:
        assert task["is_critical"] is False
        assert task["total_float"] > 0
        assert task["id"] not in critical


def test_near_critical_is_sorted_tightest_first():
    payload = _call(
        {
            "start": START.isoformat(),
            "include_near_critical": True,
            "near_critical_threshold": 10.0,
        }
    )
    floats = [t["total_float"] for t in payload["near_critical"]]
    assert floats == sorted(floats)


def test_an_out_of_range_threshold_is_rejected():
    for bad in (0, -1, 61):
        result = _as_a_client(
            {"include_near_critical": True, "near_critical_threshold": bad}
        )
        assert result.is_error is True
        assert "[invalid_argument]" in result.content[0].text


# --- provenance, routing, and size ------------------------------------------


def test_the_response_carries_provenance():
    payload = _call()
    assert payload["ref"]["canonical_ref"]
    assert payload["ref"]["content_hash"]
    assert payload["computed_at"]
    assert payload["start_date"]


def test_the_description_steers_away_from_plan_forecast():
    """Descriptions are a prompt surface: a model that conflates 'what drives
    the date' with 'how likely is the date' answers confidently and wrongly."""

    async def run():
        async with Client(build_server()) as client:
            return await client.list_tools()

    tools = asyncio.run(run()).tools
    description = next(t for t in tools if t.name == "plan.critical_path").description
    assert "plan.forecast" in description
    assert "deterministic" in description
    assert "NOT a probability" in description


def test_the_response_stays_bounded():
    """Bigger than plan.get's summary by design — it returns per-task detail —
    but it must not drift toward the full 41 KB Gantt payload."""
    assert len(json.dumps(_call({"start": START.isoformat()}))) < 16_000


def test_no_per_task_provenance_leaks_in():
    blob = json.dumps(_call({"start": START.isoformat()}))
    assert "source_quote" not in blob
