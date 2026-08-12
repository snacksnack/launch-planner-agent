"""The read-only boundary, swept at call level (RC1-243).

Three mechanisms guard the epic's "read + simulate only" claim, and they cover
different things:

1. the **import contract** (`mcp_server is read-only`) stops the package
   reaching `app.cli`, `agents`, or `anthropic` at all;
2. the **allowlist** (`test_allowlist.py`) stops a tool being exposed without
   someone consciously listing it;
3. **this file** stops a *call* being made through a module that is legitimately
   reachable.

The third is not implied by the first two, and that is the whole reason this
story survived after the other mechanisms moved forward into RC1-236.
`SQLiteEventStore` is imported by `resolve.py` and `health.py` because reads
need it — and `append`, `save_scenario` and `delete_scenario` hang off the same
class. No contract can tell those apart; only patching them and exercising every
tool can.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from app.config import get_settings
from app.store import SQLiteEventStore
from mcp.client.client import Client
from mcp_server import drift as drift_client
from mcp_server.allowlist import TOOL_ALLOWLIST
from mcp_server.server import build_server
from mcp_server.tools.drift import encode_finding_id
from planner_core import Plan, Snapshot, SnapshotKind, content_hash

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = REPO_ROOT / "fixtures/jira-cloud-migration/golden/expected-plan.json"
START = "2026-08-03"

#: The canonical surface. RC1-243 owns this list; the allowlist must equal it.
EXPECTED_TOOLS = {
    "platform.health",
    "plan.list",
    "plan.get",
    "plan.critical_path",
    "plan.simulate",
    "plan.forecast",
    "drift.check",
    "drift.explain",
    "status.draft",
}

_FINDING = {
    "rule_type": "timeline_inversion",
    "upstream": "RC1-157",
    "downstream": "RC1-158",
    "severity": 48.0,
    "severity_bucket": "red",
    "detail": "RC1-157 due 2026-07-20 lands after RC1-158 start/due 2026-07-08.",
    "first_seen_run": 1,
    "is_new": True,
}
_FINDING_ID = encode_finding_id("timeline_inversion", "RC1-157", "RC1-158")

#: Every tool with arguments that make it do real work. A tool that errors out
#: proves nothing about whether it would have written.
EVERY_TOOL: list[tuple[str, dict]] = [
    ("platform.health", {}),
    ("plan.list", {}),
    ("plan.get", {"start": START}),
    ("plan.get", {"start": START, "detail": True}),
    ("plan.critical_path", {"start": START, "include_near_critical": True}),
    ("plan.simulate", {"start": START, "task": "legal review", "days": 30}),
    ("plan.simulate", {"start": START, "task": "legal review", "days": 2}),
    ("plan.forecast", {"start": START, "iterations": 100}),
    ("status.draft", {"start": START, "current": "2"}),
    ("drift.check", {}),
    ("drift.explain", {"finding_id": _FINDING_ID}),
]


@pytest.fixture
def platform(monkeypatch):
    """A store with real history and a mocked drift service, so every tool works."""
    plan = Plan.model_validate_json(GOLDEN.read_text())
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        store.append(
            Snapshot(
                kind=SnapshotKind.BASELINE,
                plan=plan,
                content_hash=content_hash(plan),
                created_at=datetime.now(UTC),
                approved_by="Priya Nair",
                message="initial plan",
            )
        )
        slipped = plan.model_copy(deep=True)
        for task in slipped.tasks:
            if task.id == "task-bulk-migration":
                task.estimate = task.estimate.model_copy(
                    update={"likely": task.estimate.likely + 10}
                )
        store.append(
            Snapshot(
                kind=SnapshotKind.COMMIT,
                plan=slipped,
                content_hash=content_hash(slipped),
                created_at=datetime.now(UTC),
                approved_by="Priya Nair",
                message="re-estimated",
            )
        )
    finally:
        store.close()

    body = {
        "project_key": "RC1",
        "run_id": 1,
        "run_at": "2026-07-02T20:37:51Z",
        "count": 1,
        "findings": [_FINDING],
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body)

    monkeypatch.setenv("LPA_DRIFT_BASE_URL", "https://drift.test")
    drift_client.get_mcp_settings.cache_clear()
    monkeypatch.setattr(
        drift_client,
        "_client",
        lambda s: httpx.Client(
            base_url="https://drift.test", transport=httpx.MockTransport(handler)
        ),
    )
    yield requests
    drift_client.get_mcp_settings.cache_clear()


def _exercise_every_tool() -> list[str]:
    """Call every tool for real. Returns the names that succeeded."""

    async def run() -> list[str]:
        succeeded: list[str] = []
        async with Client(build_server()) as client:
            for name, args in EVERY_TOOL:
                result = await client.call_tool(name, args)
                assert result.is_error is False, f"{name} failed: {result.content}"
                succeeded.append(name)
        return succeeded

    return asyncio.run(run())


# --- the surface ------------------------------------------------------------


def test_the_registered_surface_is_exactly_the_nine_tools():
    async def run():
        async with Client(build_server()) as client:
            return await client.list_tools()

    registered = {tool.name for tool in asyncio.run(run()).tools}
    assert registered == EXPECTED_TOOLS
    assert registered == set(TOOL_ALLOWLIST)


def test_the_sweep_actually_exercises_every_tool(platform):
    """Guards the sweep itself: if a tool stopped being called, the write
    assertions below would pass for the wrong reason."""
    assert set(_exercise_every_tool()) == EXPECTED_TOOLS


# --- no writes reach the store ----------------------------------------------


def test_no_tool_writes_to_the_plan_store(platform, monkeypatch):
    """The gap neither the import contract nor the allowlist covers.
    `SQLiteEventStore` is legitimately imported for reads, and the write methods
    hang off the same class."""
    for method in ("append", "save_scenario", "delete_scenario"):
        monkeypatch.setattr(
            SQLiteEventStore,
            method,
            lambda *a, _m=method, **kw: pytest.fail(f"a tool called store.{_m}"),
        )
    _exercise_every_tool()


def test_the_write_sentinels_actually_fire(platform, monkeypatch):
    """Proves the test above can fail. A sentinel that never triggers is a green
    light that means nothing.

    `pytest.fail` raises a BaseException subclass, which matters here: both
    `legible_errors` and the MCP SDK catch `Exception`, so a sentinel tripped
    inside a tool propagates out instead of being converted into a tidy
    `is_error=True` result the sweep would read as success. Don't "fix" these
    sentinels to raise a normal exception.
    """
    monkeypatch.setattr(
        SQLiteEventStore,
        "append",
        lambda *a, **kw: pytest.fail("sentinel fired"),
    )
    with pytest.raises(pytest.fail.Exception, match="sentinel fired"):
        store = SQLiteEventStore(get_settings().sqlite_path)
        store.append(None)


def test_the_snapshot_history_is_unchanged_after_a_full_sweep(platform):
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        before = [(s.version, s.content_hash) for s in store.history()]
    finally:
        store.close()

    _exercise_every_tool()

    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        assert [(s.version, s.content_hash) for s in store.history()] == before
        assert store.list_scenarios() == []
    finally:
        store.close()


# --- no writes reach Jira, and no LLM is called -----------------------------


def test_no_tool_touches_jira(platform, monkeypatch):
    import app.jira_client as jira

    for method in ("create_issue", "update_issue"):
        monkeypatch.setattr(
            jira.RealJiraTarget,
            method,
            lambda *a, _m=method, **kw: pytest.fail(f"a tool called Jira {_m}"),
        )
    _exercise_every_tool()


def test_no_tool_reaches_the_llm_sdk(platform):
    """The import contract forbids `anthropic` statically; this is the runtime
    half, and it is stronger than patching the client constructor: after
    exercising every tool the SDK must not even have been imported.

    Deliberately does not `import anthropic` to patch it — doing so would put it
    in `sys.modules` and break `test_planner_core_has_no_llm_dependency`, which
    makes exactly this assertion about the deterministic core. Comparing before
    and after keeps the check independent of test ordering.
    """
    import sys

    before = "anthropic" in sys.modules
    _exercise_every_tool()
    assert ("anthropic" in sys.modules) == before, "a tool imported the Anthropic SDK"


# --- the drift service is only ever read ------------------------------------


def test_every_drift_request_is_a_read(platform):
    """`POST /drift/run` collects from Jira, calls Anthropic, and DMs owners on
    Slack. Re-asserted here across the whole surface, not just the drift tools."""
    _exercise_every_tool()

    assert platform, "the drift service was never called"
    for request in platform:
        assert request.method == "GET", f"{request.method} {request.url}"
        assert "/drift/run" not in str(request.url)


# --- defence in depth: demo semantics ---------------------------------------


def test_the_whole_surface_works_under_public_demo_semantics(platform, monkeypatch):
    """`LPA_PUBLIC_DEMO` is what makes the deployed API refuse scenario writes.
    Running the tools under it proves nothing here depends on write access."""
    monkeypatch.setenv("LPA_PUBLIC_DEMO", "true")
    get_settings.cache_clear()
    try:
        assert set(_exercise_every_tool()) == EXPECTED_TOOLS
    finally:
        get_settings.cache_clear()


# --- the plan file on disk is never modified --------------------------------


def test_the_golden_plan_file_is_untouched(platform):
    before = GOLDEN.read_bytes()
    _exercise_every_tool()
    assert GOLDEN.read_bytes() == before


def test_no_tool_creates_the_database(tmp_path, monkeypatch):
    """RC1-247: reads must not bring a store into existence.

    `SQLiteEventStore` migrates on construction, so opening a missing path
    creates an empty database — a write, from a server whose whole claim is that
    it does not write. `platform.health` always guarded this; `plan.list` did
    not, and every read path now shares one check.

    Not part of the `platform` fixture's sweep, because that fixture seeds a
    store on purpose. Only the tools that work without one are exercised here.
    """
    from app.config import get_settings

    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'absent.db'}")
    monkeypatch.setenv("LPA_DRIFT_BASE_URL", "")
    get_settings.cache_clear()
    drift_client.get_mcp_settings.cache_clear()
    try:
        assert not (tmp_path / "absent.db").exists()

        async def run():
            async with Client(build_server()) as client:
                for name, args in EVERY_TOOL:
                    if name in {"status.draft", "drift.check", "drift.explain"}:
                        continue  # these need a baseline or the drift service
                    await client.call_tool(name, args)

        asyncio.run(run())

        assert not (tmp_path / "absent.db").exists(), "a read tool created the database"
    finally:
        get_settings.cache_clear()
        drift_client.get_mcp_settings.cache_clear()
