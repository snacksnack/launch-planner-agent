"""`status.draft`.

Composition, not new analysis — so the tests are about faithfulness: the facts
must match what `/api/status` already returns, the health signal must stay a
rule rather than prose, and the absence of a baseline must never be reported as
the absence of change.
"""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.config import get_settings
from app.main import create_app
from app.store import SQLiteEventStore
from fastapi.testclient import TestClient
from mcp.client.client import Client
from mcp_server.server import build_server
from planner_core import Plan, Snapshot, SnapshotKind, content_hash

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = REPO_ROOT / "fixtures/jira-cloud-migration/golden/expected-plan.json"
START = "2026-08-03"
SLIP_DAYS = 10


def _golden() -> Plan:
    return Plan.model_validate_json(GOLDEN.read_text())


def _append(plan: Plan, kind: SnapshotKind, message: str) -> Snapshot:
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        return store.append(
            Snapshot(
                kind=kind,
                plan=plan,
                content_hash=content_hash(plan),
                created_at=datetime.now(UTC),
                approved_by="Priya Nair",
                message=message,
            )
        )
    finally:
        store.close()


@pytest.fixture
def baselined_and_slipped():
    """v1 baseline, v2 with the bulk migration re-estimated later.

    A real drift rather than a synthetic one: the slipped task is on the
    critical path, so the launch date moves and the health rule has something
    to fire on.
    """
    base = _golden()
    _append(base, SnapshotKind.BASELINE, "initial plan")

    current = base.model_copy(deep=True)
    for task in current.tasks:
        if task.id == "task-bulk-migration":
            task.estimate = task.estimate.model_copy(
                update={"likely": task.estimate.likely + SLIP_DAYS}
            )
    _append(current, SnapshotKind.COMMIT, "re-estimated after the pilot")
    return base, current


def _call(args: dict | None = None) -> dict:
    result = asyncio.run(
        build_server().call_tool("status.draft", {"start": START, **(args or {})})
    )
    assert result.is_error is False, result.content
    return result.structured_content


def _as_a_client(args: dict):
    async def run():
        async with Client(build_server()) as client:
            return await client.call_tool("status.draft", {"start": START, **args})

    return asyncio.run(run())


# --- the no-baseline case ---------------------------------------------------


def test_no_baseline_is_an_explicit_error_not_an_empty_update():
    """`/api/status` returns {"baseline": null}. A model narrates an empty
    result as 'no changes this week', which is the opposite of the truth."""
    result = _as_a_client({})
    assert result.is_error is True
    text = result.content[0].text
    assert "[plan_not_found]" in text
    assert "nothing to compare" in text
    assert "not the same as 'nothing changed'" in text
    assert "plan baseline" in text


def test_the_no_baseline_message_does_not_suggest_falling_back_to_the_default():
    """The resolver's own advice — omit the reference and use the default plan —
    is right for plan.get and misleading here, where the only fix is to commit
    a baseline."""
    text = _as_a_client({}).content[0].text
    assert "Omit the reference" not in text


def test_a_commit_without_a_baseline_still_fails_cleanly():
    _append(_golden(), SnapshotKind.COMMIT, "committed, never baselined")
    result = _as_a_client({})
    assert result.is_error is True
    assert "No baseline has been committed" in result.content[0].text


# --- parity with the API ----------------------------------------------------


def test_the_facts_match_the_api_for_the_same_refs(baselined_and_slipped):
    """The acceptance criterion. Both call the same planner_core functions, so
    any divergence is in the wrapping — which is what this catches."""
    payload = _call({"current": "2"})

    client = TestClient(create_app())
    api = client.get("/api/status", params={"start": START, "current": "2"}).json()
    api_facts = api["facts"]

    facts = payload["facts"]
    assert facts["health"] == api_facts["health"]
    assert facts["health_reasons"] == api_facts["health_reasons"]
    assert facts["launch_before"] == api_facts["launch_before"]
    assert facts["launch_after"] == api_facts["launch_after"]
    assert facts["launch_shift_working_days"] == api_facts["launch_shift_days"]
    assert facts["structural_change_count"] == api_facts["structural_change_count"]

    for ours, theirs in (
        ("slipped", "slipped"),
        ("newly_critical", "newly_critical"),
        ("no_longer_critical", "no_longer_critical"),
    ):
        assert [t["id"] for t in facts[ours]] == [t["id"] for t in api_facts[theirs]]
        assert [t["shift_working_days"] for t in facts[ours]] == [
            t["shift_days"] for t in api_facts[theirs]
        ]

    assert [m["id"] for m in facts["milestone_drift"]] == [
        m["id"] for m in api_facts["milestone_drift"]
    ]
    assert payload["exec_summary"] == api["narrative"]["exec_summary"]
    assert payload["points"] == api["narrative"]["points"]
    assert payload["markdown"] == api["markdown"]


def test_no_fact_the_api_reports_is_silently_dropped(baselined_and_slipped):
    """A generic flattening helper lost shift_days and severity in an earlier
    draft. This asserts every populated API fact survives the wrapping."""
    client = TestClient(create_app())
    api_facts = client.get(
        "/api/status", params={"start": START, "current": "2"}
    ).json()["facts"]
    ours = _call({"current": "2"})["facts"]

    renamed = {"launch_shift_days": "launch_shift_working_days"}
    for key, value in api_facts.items():
        mapped = renamed.get(key, key)
        if isinstance(value, list) and value:
            assert ours[mapped], f"{key} is populated in the API but empty here"
            assert len(ours[mapped]) == len(value)


# --- health is a rule -------------------------------------------------------


def test_a_critical_path_slip_flips_health_by_rule(baselined_and_slipped):
    """ADR-0019: a launch slip of 10 or more working days is red, decided by
    rule and not by whatever the narrative says."""
    facts = _call({"current": "2"})["facts"]
    assert facts["launch_shift_working_days"] == SLIP_DAYS
    assert facts["health"] == "red"
    assert facts["health_reasons"]


def test_comparing_a_plan_against_itself_is_green(baselined_and_slipped):
    facts = _call({"current": "1"})["facts"]
    assert facts["launch_shift_working_days"] == 0
    assert facts["health"] == "green"


def test_the_narrative_agrees_with_the_facts(baselined_and_slipped):
    payload = _call({"current": "2"})
    assert str(SLIP_DAYS) in payload["exec_summary"]
    assert payload["facts"]["launch_after"] in payload["exec_summary"]


# --- provenance and labelling ----------------------------------------------


def test_narrative_source_is_present_and_says_deterministic(baselined_and_slipped):
    """The LLM narrative comes from the gated CLI path, which the import
    contract forbids this package from reaching. The two read alike, so the
    source is stated rather than left to be guessed."""
    payload = _call({"current": "2"})
    assert payload["narrative_source"] == "deterministic"


def test_both_plan_references_are_reported(baselined_and_slipped):
    payload = _call({"current": "2"})
    assert payload["current"]["version"] == 2
    assert payload["baseline"]["version"] == 1
    assert payload["baseline"]["kind"] == "baseline"
    assert payload["baseline_note"] == "initial plan"
    assert payload["computed_at"]


def test_an_explicit_baseline_reference_is_honoured(baselined_and_slipped):
    payload = _call({"current": "2", "baseline": "1"})
    assert payload["baseline"]["version"] == 1


def test_the_period_label_is_carried_through(baselined_and_slipped):
    assert _call({"current": "2", "period": "Sprint 14"})["facts"]["period_label"] == "Sprint 14"


# --- it drafts, it does not send -------------------------------------------


def test_the_response_says_it_was_not_sent(baselined_and_slipped):
    assert _call({"current": "2"})["sent"] is False


def test_the_module_imports_nothing_that_could_deliver_anything():
    """ADR-0019 left sending to a scheduling concern. Checked against the source
    rather than by mocking, because the risk is an import someone adds later."""
    from mcp_server.tools import status as status_module

    tree = ast.parse(Path(status_module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("smtplib", "email", "httpx", "requests", "slack_sdk"):
        assert forbidden not in imported, f"status.py imports {forbidden}"


def test_nothing_is_written_to_the_store(baselined_and_slipped):
    before = len(SQLiteEventStore(get_settings().sqlite_path).history())
    _call({"current": "2"})
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        assert len(store.history()) == before
        assert store.list_scenarios() == []
    finally:
        store.close()


# --- shape ------------------------------------------------------------------


def test_the_response_is_bounded(baselined_and_slipped):
    payload = _call({"current": "2"})
    assert len(json.dumps(payload)) < 12_000
    assert payload["facts"]["truncated_lists"] == []


def test_the_markdown_is_ready_to_paste(baselined_and_slipped):
    markdown = _call({"current": "2"})["markdown"]
    assert markdown.strip()
    assert "Bulk-migrate" in markdown


def test_the_description_states_it_never_sends():
    async def run():
        async with Client(build_server()) as client:
            return await client.list_tools()

    tools = asyncio.run(run()).tools
    description = next(t for t in tools if t.name == "status.draft").description
    assert "sends no email" in description
    assert "decided by rule" in description
    assert "opposite things" in description
