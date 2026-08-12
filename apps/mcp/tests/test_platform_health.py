"""`platform.health` — the states it reports, and the ones it must not raise on."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.store import SQLiteEventStore
from mcp_server.config import get_mcp_settings
from mcp_server.drift import DriftStatus
from mcp_server.server import build_server
from mcp_server.tools import health as health_module
from planner_core import Plan, Snapshot, SnapshotKind, content_hash


def _call() -> dict:
    server = build_server()
    result = asyncio.run(server.call_tool("platform.health", {}))
    assert result.is_error is False
    return result.structured_content


def _seed_one_snapshot() -> None:
    """Commit a snapshot so the store has history to report."""
    golden = (
        Path(__file__).resolve().parents[3]
        / "fixtures/jira-cloud-migration/golden/expected-plan.json"
    )
    plan = Plan.model_validate_json(golden.read_text())
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        store.append(
            Snapshot(
                kind=SnapshotKind.COMMIT,
                plan=plan,
                content_hash=content_hash(plan),
                created_at=datetime.now(UTC),
                approved_by="test",
                message="seed",
            )
        )
    finally:
        store.close()


def test_missing_store_is_healthy_and_creates_nothing():
    """A health check must not write. `SQLiteEventStore` migrates on construction,
    so probing a path that isn't there would create an empty database."""
    sqlite_path = Path(get_settings().sqlite_path)
    assert not sqlite_path.exists()

    payload = _call()

    assert payload["plan_store"]["state"] == "ok"
    assert "no plan store" in payload["plan_store"]["detail"]
    assert not sqlite_path.exists(), "health check created the database"


def test_populated_store_reports_its_snapshot_count():
    _seed_one_snapshot()
    payload = _call()
    assert payload["plan_store"]["state"] == "ok"
    assert "1 snapshot(s)" in payload["plan_store"]["detail"]


def test_drift_not_configured_is_reported_not_raised():
    payload = _call()
    assert payload["drift_service"]["state"] == "not_configured"
    assert "LPA_DRIFT_BASE_URL" in payload["drift_service"]["detail"]


def test_drift_down_is_reported_and_the_server_stays_healthy(monkeypatch):
    """The acceptance criterion: one dead upstream must not make the whole
    server look down, and must not raise."""
    monkeypatch.setattr(
        health_module,
        "probe",
        lambda *a, **kw: DriftStatus(
            configured=True, reachable=False, detail="unreachable at https://drift.test"
        ),
    )
    payload = _call()
    assert payload["drift_service"]["state"] == "unavailable"
    assert "unreachable" in payload["drift_service"]["detail"]
    # The rest of the response is still good.
    assert payload["plan_store"]["state"] == "ok"
    assert payload["server_version"]


def test_drift_up_is_reported_ok(monkeypatch):
    monkeypatch.setattr(
        health_module,
        "probe",
        lambda *a, **kw: DriftStatus(
            configured=True, reachable=True, detail="reachable at https://drift.test"
        ),
    )
    assert _call()["drift_service"]["state"] == "ok"


def test_response_carries_a_fresh_timestamp():
    before = datetime.now(UTC)
    checked_at = datetime.fromisoformat(_call()["checked_at"])
    assert before <= checked_at <= datetime.now(UTC)


def test_health_takes_no_arguments():
    """It must be callable with an empty object — a model should never have to
    guess a parameter to run a diagnostic."""
    server = build_server()
    tools = asyncio.run(server.list_tools())
    health = next(t for t in tools if t.name == "platform.health")
    assert health.input_schema.get("properties") == {}
    assert not health.input_schema.get("required")


def test_settings_are_isolated_from_the_developers_own_store(tmp_path):
    """Guards the conftest fixture itself: a leak here would make every
    assertion above depend on whoever ran the suite."""
    assert get_mcp_settings().drift_configured is False
    assert Path(get_settings().sqlite_path).is_relative_to(tmp_path)
