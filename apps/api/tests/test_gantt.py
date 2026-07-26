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


def test_default_plan_resolves_regardless_of_cwd(tmp_path, monkeypatch):
    # uvicorn is often launched from apps/api, not the repo root — the default
    # relative plan_path must still resolve (via the repo-root fallback).
    monkeypatch.chdir(tmp_path)
    resp = TestClient(create_app()).get("/api/plan")
    assert resp.status_code == 200
    assert len(resp.json()["tasks"]) == 23
