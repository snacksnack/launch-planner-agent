from app.config import Settings, get_settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_healthz_returns_ok():
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "launch-planner-agent"


def test_sanity_check_masks_credentials():
    settings = Settings(anthropic_api_key="secret-should-not-appear")
    summary = settings.sanity_check()
    assert summary["anthropic_api_key"] == "set"
    assert "secret-should-not-appear" not in str(summary)


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
