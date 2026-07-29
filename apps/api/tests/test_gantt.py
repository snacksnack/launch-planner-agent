"""Tests for the Gantt payload builder and the /api/plan endpoint.

The frontend can't be exercised here, so the data contract it consumes is what
we lock down: correct shape, critical-path flags, and — the point of the whole
project — provenance surfaced on tasks and on dependency edges.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.gantt import build_gantt_payload
from app.main import create_app
from fastapi.testclient import TestClient
from planner_core import Plan, schedule_plan

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "jira-cloud-migration"
GOLDEN = FIXTURE / "golden" / "expected-plan.json"
MONDAY = date(2026, 8, 3)


def _golden_payload() -> dict:
    plan = Plan.model_validate_json(GOLDEN.read_text())
    return build_gantt_payload(plan, schedule_plan(plan, start_date=MONDAY))


def test_payload_has_expected_top_level_shape():
    payload = _golden_payload()
    assert set(payload) >= {"project", "epics", "tasks", "milestones", "deadlines", "freezes"}
    assert payload["project"]["critical_path_ids"]
    assert payload["project"]["finish_date"]
    assert len(payload["tasks"]) == 23
    assert len(payload["epics"]) == 6


def test_tasks_carry_dates_epic_owner_and_provenance():
    payload = _golden_payload()
    task = next(t for t in payload["tasks"] if t["id"] == "task-inventory")
    assert task["start"] and task["end"]
    assert task["epic_name"] == "Assessment & Planning"
    assert task["owner_name"] == "Tomás Rivera"
    assert task["estimate"] == {"optimistic": 3, "likely": 5, "pessimistic": 8}
    # The audit trail is present on the bar.
    assert "source_quote" in task["provenance"]
    assert task["provenance"]["confidence"] in {"high", "medium", "low"}


def test_jira_key_and_url_surface_once_a_task_is_pushed():
    """RC1-200: a task with a jira_key gets a clickable browse URL in the payload."""
    plan = Plan.model_validate_json(GOLDEN.read_text())
    plan.tasks[0].jira_key = "PMA-42"
    plan.epics[0].jira_key = "PMA-1"
    schedule = schedule_plan(plan, start_date=MONDAY)

    # With a site URL, the key becomes a browse link.
    payload = build_gantt_payload(
        plan, schedule, jira_base_url="https://acme.atlassian.net/"
    )
    pushed = next(t for t in payload["tasks"] if t["id"] == plan.tasks[0].id)
    assert pushed["jira_key"] == "PMA-42"
    assert pushed["jira_url"] == "https://acme.atlassian.net/browse/PMA-42"
    assert payload["epics"][0]["jira_url"] == "https://acme.atlassian.net/browse/PMA-1"
    # An un-pushed task carries no key or link.
    other = next(t for t in payload["tasks"] if t["id"] == plan.tasks[1].id)
    assert other["jira_key"] is None and other["jira_url"] is None

    # Without a site URL configured, the key shows but there's no link.
    no_base = build_gantt_payload(plan, schedule)
    assert no_base["tasks"][0]["jira_key"] == "PMA-42"
    assert no_base["tasks"][0]["jira_url"] is None


def test_critical_path_tasks_are_flagged():
    payload = _golden_payload()
    critical_ids = set(payload["project"]["critical_path_ids"])
    assert critical_ids
    for task in payload["tasks"]:
        assert task["is_critical"] is (task["id"] in critical_ids)


def test_buried_legal_constraint_surfaces_as_a_dependency_with_quote():
    """AC2: the legal gate is visible as an edge carrying its verbatim quote."""
    payload = _golden_payload()
    pilot = next(t for t in payload["tasks"] if t["id"] == "task-pilot-migration")
    legal_edges = [p for p in pilot["predecessors"] if p["from"] == "task-legal-review"]
    assert legal_edges, "pilot migration should depend on legal review"
    quote = legal_edges[0]["provenance"]["source_quote"]
    assert "Legal has to sign" in quote


def test_payload_carries_the_raid_log_with_severity_and_evidence():
    """RC1-191: the RAID log rides the payload, with derived severity + owner name."""
    payload = _golden_payload()
    raid = payload["raid"]
    assert len(raid) == 5
    by_id = {r["id"]: r for r in raid}

    risk = by_id["raid-single-owner"]
    assert risk["type"] == "risk"
    assert risk["severity"] == 12  # probability 3 x impact 4
    assert risk["suggested_owner_name"] == "Priya Nair"
    assert risk["provenance"]["evidence"]["kind"] == "schedule"
    assert risk["provenance"]["evidence"]["fact_code"] == "single-owner-critical-path"

    # A PRD-sourced item carries its verbatim quote.
    plugin = by_id["raid-plugin-incompat"]
    assert plugin["provenance"]["evidence"]["kind"] == "prd"
    assert "no direct Cloud equivalent" in plugin["provenance"]["evidence"]["source_quote"]


def test_deadline_check_present_for_hard_date_constraint():
    payload = _golden_payload()
    checks = {c["task_id"]: c for c in payload["deadlines"]}
    assert "task-decom-onprem" in checks
    assert checks["task-decom-onprem"]["met"] is True


def test_api_plan_endpoint_serves_the_golden_by_default():
    client = TestClient(create_app())
    resp = client.get("/api/plan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["name"].startswith("On-Prem Jira")
    assert len(body["tasks"]) == 23


def test_api_plan_includes_the_decision_record():
    """RC1-197: the decisions/validation audit rides the payload (recomputed here,
    since the golden is a plan file with a resolvable sibling PRD)."""
    body = TestClient(create_app()).get("/api/plan").json()
    decisions = body["decisions"]
    assert set(decisions) == {"rejected_edges", "cycle_breaks", "flagged", "coverage_gaps"}
    # The golden is hand-authored clean, but its inferred entities are low-confidence.
    low = [f for f in decisions["flagged"] if f["code"] == "low-confidence"]
    assert {f["entity_id"] for f in low} >= {"task-cutover-rehearsal", "task-closeout"}
    # PRD is resolvable, so source-dependent coverage is computed (non-empty).
    assert decisions["coverage_gaps"]


def test_api_plan_rejects_missing_plan_and_bad_date():
    client = TestClient(create_app())
    assert client.get("/api/plan", params={"plan": "/nope.json"}).status_code == 404
    assert client.get("/api/plan", params={"start": "not-a-date"}).status_code == 400


def test_api_simulate_returns_baseline_simulated_and_delta():
    """RC1-190: POST a scenario → baseline + simulated payloads + schedule delta."""
    client = TestClient(create_app())
    resp = client.post(
        "/api/simulate",
        json={"changes": [{"kind": "delay_task", "task_id": "task-legal-review", "days": 30}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"baseline", "simulated", "delta", "warnings"}
    assert len(body["simulated"]["tasks"]) == 23
    delta = body["delta"]
    # Legal review has 6 working days of float; a 30-day slip overflows by 24.
    assert delta["finish_shift_days"] == 24
    assert any(n["id"] == "task-legal-review" for n in delta["critical_joined"])


def test_api_simulate_absorbed_slip_reports_zero_impact():
    client = TestClient(create_app())
    resp = client.post(
        "/api/simulate",
        json={"changes": [{"kind": "delay_task", "task_id": "task-inventory", "days": 1}]},
    )
    body = resp.json()
    assert body["delta"]["finish_shift_days"] == 0
    assert "absorbed by available float" in body["delta"]["headline"]


def test_api_jira_returns_the_mock_generation_plan():
    """RC1-193: /api/jira serves the mock preview (read-only, no writes)."""
    body = TestClient(create_app()).get("/api/jira").json()
    assert body["creates"] == 29  # 6 epics + 23 stories
    assert body["links"] == 28
    assert body["has_credentials"] is False  # no creds in tests
    gen = body["generation"]
    assert gen["project_key"] == "PMA"
    epics = [op for op in gen["issues"] if op["issue_type"] == "Epic"]
    assert len(epics) == 6
    story = next(op for op in gen["issues"] if op["local_id"] == "task-inventory")
    assert story["due_date"]  # scheduled finish date
    assert "Reasoning:" in story["description"]  # provenance travels in


def test_api_status_reports_no_baseline_on_an_empty_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert TestClient(create_app()).get("/api/status").json() == {"baseline": None}
    finally:
        get_settings.cache_clear()


def test_api_status_returns_facts_health_and_rendered_report(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    from app.config import get_settings
    from app.store import SQLiteEventStore
    from planner_core import Plan, ThreePointEstimate, commit_baseline

    get_settings.cache_clear()
    try:
        # Baseline is an optimistic version; the current golden reads as drift.
        golden = Plan.model_validate_json(GOLDEN.read_text())
        base = golden.model_copy(deep=True)
        for t in base.tasks:
            if t.id == "task-bulk-migration":
                e = t.estimate
                t.estimate = ThreePointEstimate(
                    optimistic=max(1, e.optimistic - 8), likely=max(1, e.likely - 8),
                    pessimistic=max(1, e.pessimistic - 8),
                )
        store = SQLiteEventStore(str(tmp_path / "plans.db"))
        commit_baseline(store, base, approved_by="R", note="initial", now=datetime.now(UTC))
        store.close()

        body = TestClient(create_app()).get("/api/status").json()
        assert body["baseline"]["version"] == 1
        assert body["facts"]["health"] in {"yellow", "red"}  # the current plan drifted
        assert body["facts"]["launch_shift_days"] > 0
        assert body["narrative"]["exec_summary"]
        assert "Status update" in body["markdown"]
        assert "<div" in body["html"]
    finally:
        get_settings.cache_clear()


def test_api_baseline_reports_no_baseline_on_an_empty_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        resp = TestClient(create_app()).get("/api/baseline")
        assert resp.status_code == 200
        assert resp.json() == {"baseline": None}
    finally:
        get_settings.cache_clear()


def test_api_baseline_returns_variance_against_a_seeded_baseline(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    from app.config import get_settings
    from app.store import SQLiteEventStore
    from planner_core import Plan, commit_baseline

    get_settings.cache_clear()
    try:
        # Baseline the golden; the "current" (default plan file) is the same golden,
        # so an unedited plan is on track (variance = zero).
        golden = Plan.model_validate_json(GOLDEN.read_text())
        store = SQLiteEventStore(str(tmp_path / "plans.db"))
        commit_baseline(store, golden, approved_by="Reid", note="initial", now=datetime.now(UTC))
        store.close()

        body = TestClient(create_app()).get("/api/baseline").json()
        assert body["baseline"]["note"] == "initial"
        assert body["baseline"]["payload"]["tasks"]
        assert body["current"]["payload"]["tasks"]
        assert body["comparison"]["is_on_track"] is True
        assert body["comparison"]["plan_diff"] == []
    finally:
        get_settings.cache_clear()


def test_default_plan_resolves_regardless_of_cwd(tmp_path, monkeypatch):
    # uvicorn is often launched from apps/api, not the repo root — the default
    # relative plan_path must still resolve (via the repo-root fallback).
    monkeypatch.chdir(tmp_path)
    resp = TestClient(create_app()).get("/api/plan")
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) == 23
