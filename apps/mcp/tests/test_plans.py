"""`plan.list` and `plan.get`, including the response-size budget.

The size assertions are the enforceable form of RC1-237's "materially smaller
than the full Gantt payload". Measured on the flagship golden at the time of
writing: summary 1,427 bytes, `detail=true` 41,684 bytes — 3.4%. The ceilings
below sit roughly 2–3x above the measurements, so ordinary field additions pass
while a regression that starts embedding tasks does not.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.store import SQLiteEventStore
from mcp.client.client import Client
from mcp_server.server import build_server
from planner_core import Plan, Snapshot, SnapshotKind, content_hash

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "fixtures/jira-cloud-migration/golden/expected-plan.json"
)

#: A summary must fit comfortably in a conversation.
MAX_SUMMARY_BYTES = 4_096
#: And must stay a small fraction of the UI payload it is derived from.
MAX_SUMMARY_RATIO = 0.10


def _call(tool: str, args: dict | None = None) -> dict:
    result = asyncio.run(build_server().call_tool(tool, args or {}))
    assert result.is_error is False, result.content
    return result.structured_content


def _seed(kind: SnapshotKind = SnapshotKind.COMMIT, name: str = "Committed") -> Snapshot:
    plan = Plan.model_validate_json(GOLDEN.read_text()).model_copy(update={"name": name})
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        return store.append(
            Snapshot(
                kind=kind,
                plan=plan,
                content_hash=content_hash(plan),
                created_at=datetime.now(UTC),
                approved_by="Priya Nair",
                message="reviewed & approved",
            )
        )
    finally:
        store.close()


# --- plan.list --------------------------------------------------------------


def test_list_on_an_empty_store_still_names_the_default_plan():
    """An empty list plus no default would leave a model with nowhere to go."""
    payload = _call("plan.list")
    assert payload["snapshots"] == []
    assert payload["default_plan"]["source"] == "file"
    assert "nothing has been committed" in payload["note"]


def test_list_returns_each_snapshot_with_an_echoable_hash():
    snapshot = _seed()
    entry = _call("plan.list")["snapshots"][0]
    assert entry["version"] == 1
    assert entry["kind"] == "commit"
    assert entry["content_hash"] == snapshot.content_hash
    assert entry["approved_by"] == "Priya Nair"


def test_list_is_ordered_oldest_first():
    _seed(name="First")
    _seed(name="Second")
    assert [s["version"] for s in _call("plan.list")["snapshots"]] == [1, 2]


# --- plan.get ---------------------------------------------------------------


def test_get_with_no_ref_summarises_the_default_plan():
    payload = _call("plan.get")
    assert payload["ref"]["source"] == "file"
    assert payload["counts"]["tasks"] == 23
    assert payload["launch_date"] == "2026-10-12"
    assert payload["duration_working_days"] == 51.0
    assert payload["critical_task_count"] == 11


def test_get_matches_the_deterministic_engine_the_cli_uses():
    """Parity is structural (same in-process call), but the wrapping can drift —
    these are the numbers ADR-0010 records for the golden."""
    payload = _call("plan.get", {"start": "2026-08-03"})
    assert payload["launch_date"] == "2026-10-12"
    assert payload["meets_all_deadlines"] is True


def test_get_resolves_a_committed_snapshot_by_version():
    _seed(name="Committed")
    payload = _call("plan.get", {"ref": "1"})
    assert payload["ref"]["source"] == "snapshot"
    assert payload["ref"]["version"] == 1
    assert payload["name"] == "Committed"


def test_every_response_carries_provenance():
    payload = _call("plan.get")
    ref = payload["ref"]
    assert ref["content_hash"]
    assert ref["canonical_ref"]
    assert payload["computed_at"]
    assert payload["start_date"]


def _call_as_a_client(tool: str, args: dict):
    """What a real MCP client receives — a failed call is `is_error=True`, not
    an exception. `server.call_tool` raises instead, so error-path assertions
    have to go through the protocol layer to be meaningful."""

    async def run():
        async with Client(build_server()) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(run())


def test_an_unresolvable_ref_is_an_error_not_a_silent_default():
    """Falling back to the default plan would answer a question about a plan the
    user never named, and read as success."""
    result = _call_as_a_client("plan.get", {"ref": "999"})
    assert result.is_error is True
    assert "[plan_not_found]" in result.content[0].text


def test_a_bad_start_date_is_rejected_as_a_bad_argument():
    result = _call_as_a_client("plan.get", {"start": "next tuesday"})
    assert result.is_error is True
    text = result.content[0].text
    assert "[invalid_argument]" in text
    assert "YYYY-MM-DD" in text


def test_an_ambiguous_ref_reaches_the_client_with_its_candidates():
    plan = Plan.model_validate_json(GOLDEN.read_text())
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        for kind in (SnapshotKind.PROPOSAL, SnapshotKind.COMMIT):
            store.append(
                Snapshot(
                    kind=kind,
                    plan=plan,
                    content_hash=content_hash(plan),
                    created_at=datetime.now(UTC),
                    approved_by="test",
                )
            )
    finally:
        store.close()

    result = _call_as_a_client("plan.get", {"ref": content_hash(plan)[:8]})
    assert result.is_error is True
    assert "[ambiguous_plan_ref]" in result.content[0].text


def test_unlinked_milestones_are_flagged_rather_than_looking_on_time():
    """A milestone with no projected date is unlinked, not early — `scheduled`
    is what stops a model reading absence as good news."""
    for milestone in _call("plan.get")["milestones"]:
        if milestone["projected_date"] is None:
            assert milestone["scheduled"] is False


# --- the size budget --------------------------------------------------------


def test_the_summary_fits_in_a_conversation():
    size = len(json.dumps(_call("plan.get")))
    assert size < MAX_SUMMARY_BYTES, f"summary grew to {size} bytes"


def test_the_summary_is_a_small_fraction_of_the_full_payload():
    summary = len(json.dumps(_call("plan.get")))
    detailed = len(json.dumps(_call("plan.get", {"detail": True})))
    ratio = summary / detailed
    assert ratio < MAX_SUMMARY_RATIO, f"summary is {ratio:.1%} of the full payload"


def test_the_detail_flag_actually_returns_the_full_payload():
    """Guards the other direction: a summary that stayed small because `detail`
    silently did nothing would pass every assertion above."""
    payload = _call("plan.get", {"detail": True})
    assert payload["gantt"] is not None
    assert len(payload["gantt"]["tasks"]) == 23
    assert len(json.dumps(payload)) > 20_000


def test_the_summary_carries_no_per_task_provenance():
    """Provenance blocks with verbatim PRD quotes are ~31 KB of the 41 KB
    payload — the specific thing that must not leak into the default."""
    blob = json.dumps(_call("plan.get"))
    assert "provenance" not in blob
    assert "source_quote" not in blob


def test_the_plan_tools_make_no_network_call():
    """RC1-237: the drift service is the only remote call in the epic. The plan
    tools import `planner_core` and `app` in process, which is what makes CLI
    parity structural — an HTTP client here would quietly reintroduce a second
    caller with its own defaults to drift.

    Checked against the source rather than by mocking, because the failure is a
    new import someone adds later, not a call in today's code path.
    """
    import ast

    from mcp_server import resolve
    from mcp_server.tools import plans

    for module in (resolve, plans):
        tree = ast.parse(Path(module.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "httpx" not in imported, f"{module.__name__} imports httpx"
        assert "requests" not in imported, f"{module.__name__} imports requests"


def test_plan_list_stays_bounded_too():
    for i in range(5):
        _seed(name=f"Plan {i}")
    assert len(json.dumps(_call("plan.list"))) < MAX_SUMMARY_BYTES
