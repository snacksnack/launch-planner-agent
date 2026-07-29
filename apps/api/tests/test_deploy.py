"""Tests for the deploy surface (RC1-195): demo mode, seed, audit, static, rate limit."""

from __future__ import annotations

from pathlib import Path

from app.main import create_app
from fastapi.testclient import TestClient
from planner_core import Plan

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "fixtures" / "jira-cloud-migration" / "golden" / "expected-plan.json"
)


def _clear():
    from app.config import get_settings

    get_settings.cache_clear()


def test_api_info_reports_read_only_posture():
    body = TestClient(create_app()).get("/api/info").json()
    assert body["writes_enabled"] is False
    assert body["agent_endpoints"] is False
    assert body["public_demo"] is False  # off by default


# --- seed ------------------------------------------------------------------


def test_seed_if_empty_seeds_a_history_once(tmp_path):
    from app.seed_demo import seed_if_empty
    from app.store import SQLiteEventStore

    db = str(tmp_path / "seed.db")
    assert seed_if_empty(db) is True
    assert seed_if_empty(db) is False  # idempotent — no second seed

    store = SQLiteEventStore(db)
    try:
        kinds = [s.kind.value for s in store.history()]
    finally:
        store.close()
    assert kinds == ["proposal", "baseline", "commit"]


def test_demo_mode_seeds_on_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'demo.db'}")
    monkeypatch.setenv("LPA_PUBLIC_DEMO", "true")
    _clear()
    try:
        client = TestClient(create_app())
        assert client.get("/api/info").json()["public_demo"] is True
        history = client.get("/api/history").json()
        assert [h["kind"] for h in history] == ["proposal", "baseline", "commit"]
    finally:
        _clear()


# --- audit trail -----------------------------------------------------------


def test_api_audit_reconstructs_the_reasoning_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'audit.db'}")
    monkeypatch.setenv("LPA_PUBLIC_DEMO", "true")
    _clear()
    try:
        body = TestClient(create_app()).get("/api/audit").json()
        by_agent = {a["agent"]: a for a in body["agents"]}
        assert by_agent["work-breakdown"]["kinds"] == {"epic": 6, "task": 23, "milestone": 4}
        assert by_agent["dependency"]["kinds"] == {"dependency": 32}
        assert by_agent["raid"]["kinds"] == {"raid": 5}
        # validation actions + the human review timeline are present
        assert "flagged" in body["decisions"]
        assert [h["kind"] for h in body["history"]] == ["proposal", "baseline", "commit"]
        assert any(h["approved_by"] == "Priya Nair (TPM)" for h in body["history"])
    finally:
        _clear()


# --- static serving & rate limit -------------------------------------------


def test_static_web_is_served_at_root_when_configured(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Planner</title>")
    monkeypatch.setenv("LPA_WEB_DIST", str(dist))
    _clear()
    try:
        resp = TestClient(create_app()).get("/")
        assert resp.status_code == 200
        assert "Planner" in resp.text
    finally:
        _clear()


def test_public_demo_rate_limits_the_api(monkeypatch):
    monkeypatch.setenv("LPA_PUBLIC_DEMO", "true")
    monkeypatch.setenv("LPA_RATE_LIMIT_PER_MINUTE", "3")
    _clear()
    try:
        client = TestClient(create_app())
        codes = [client.get("/api/info").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert 429 in codes[3:]  # capped after 3/min
    finally:
        _clear()


def test_golden_still_loads_as_a_plan():
    # guards the seed's assumptions about the flagship fixture
    plan = Plan.model_validate_json(GOLDEN.read_text())
    assert len(plan.epics) == 6 and len(plan.tasks) == 23 and len(plan.raid) == 5
    assert plan.tasks[0].provenance.agent  # provenance present for the audit view
