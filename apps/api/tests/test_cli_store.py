"""End-to-end CLI test for the review/commit flow against a temporary store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.cli import main
from planner_core import (
    Confidence,
    Plan,
    Provenance,
    Task,
    TeamMember,
    ThreePointEstimate,
)

NOW = datetime(2026, 7, 24, tzinfo=UTC)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the CLI's store at a throwaway SQLite file (and reset the cache)."""
    from app.config import get_settings

    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _plan(name: str, *, owner: str = "tm-1", likely: float = 5) -> Plan:
    prov = Provenance(
        reasoning="r", source_quote="q", source_section=None, confidence=Confidence.HIGH,
        agent="a", model="m", timestamp=NOW,
    )
    est = ThreePointEstimate(optimistic=1, likely=likely, pessimistic=9)
    return Plan(
        id="p", name=name, team=[TeamMember(id="tm-1", name="Ada")],
        tasks=[Task(id="a", name="a", owner_id=owner, estimate=est, provenance=prov)],
    )


def _write(path: Path, plan: Plan) -> str:
    path.write_text(plan.model_dump_json())
    return str(path)


def test_propose_commit_history_diff_flow(temp_db, capsys):
    proposal = _write(temp_db / "proposal.json", _plan("proposed", likely=5))
    reviewed = _write(temp_db / "reviewed.json", _plan("reviewed", likely=8))  # human edit

    assert main(["propose", proposal]) == 0
    assert main(["commit", reviewed, "--by", "Reid", "--from", "1", "-m", "reviewed"]) == 0

    assert main(["history"]) == 0
    history_out = capsys.readouterr().out
    assert "[proposal]" in history_out and "[commit]" in history_out
    assert "by Reid" in history_out

    # Diff the recorded proposal (v1) against the committed plan (v2).
    assert main(["diff", "1", "2"]) == 0
    diff_out = capsys.readouterr().out
    assert "likely" in diff_out  # the human's estimate override is in the audit trail


def test_commit_rejected_for_invalid_plan(temp_db, capsys):
    bad = _write(temp_db / "bad.json", _plan("bad", owner="ghost"))  # owner not in team
    assert main(["commit", bad, "--by", "Reid"]) == 1
    err = capsys.readouterr().err
    assert "commit rejected" in err
    assert "unknown-owner" in err


def test_commit_requires_approver(temp_db):
    plan = _write(temp_db / "p.json", _plan("p"))
    # argparse enforces --by; omitting it exits non-zero before our code runs.
    with pytest.raises(SystemExit):
        main(["commit", plan])


def test_api_serves_committed_snapshot(temp_db):
    from app.main import create_app
    from fastapi.testclient import TestClient

    reviewed = _write(temp_db / "r.json", _plan("reviewed"))
    assert main(["commit", reviewed, "--by", "Reid", "-m", "go"]) == 0

    client = TestClient(create_app())
    history = client.get("/api/history").json()
    assert len(history) == 1 and history[0]["kind"] == "commit"

    payload = client.get("/api/plan", params={"snapshot": "1"}).json()
    assert payload["project"]["name"] == "reviewed"

    assert client.get("/api/plan", params={"snapshot": "999"}).status_code == 404
