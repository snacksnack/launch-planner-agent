"""Request counting to Datadog (RC1-455).

Credential-free: no test reaches the network. `post` is the only function that
would, and it is replaced wherever a test exercises the path through it.
"""

from __future__ import annotations

import pytest
from app import telemetry
from app.main import create_app
from fastapi.testclient import TestClient


class FakeRequest:
    """The two things `record` reads off a request: the path, and the route."""

    def __init__(self, path: str, route_path: str | None = None):
        self.url = type("U", (), {"path": path})()
        self.scope = {"route": type("R", (), {"path": route_path})()} if route_path else {}


@pytest.mark.parametrize(
    "status,expected",
    [(200, "ok"), (201, "ok"), (304, "ok"), (400, "client_error"),
     (429, "client_error"), (500, "server_error"), (503, "server_error")],
)
def test_outcome_buckets_status_codes(status, expected):
    assert telemetry.outcome_for(status) == expected


def test_route_template_prefers_the_route_over_the_resolved_path():
    """Two visitors' scenarios must not become two series."""
    req = FakeRequest("/api/scenarios/mine", route_path="/api/scenarios/{name}")
    assert telemetry.route_template(req, "/api/scenarios/mine") == "/api/scenarios/{name}"


def test_route_template_folds_unmatched_paths_into_one_bucket():
    """A 404 for a URL no route claims is unbounded; it gets one tag value."""
    assert telemetry.route_template(FakeRequest("/nope"), "/nope") == "unmatched"
    assert telemetry.route_template(FakeRequest("/also/nope"), "/also/nope") == "unmatched"


def test_env_is_normalized_to_the_estate_convention():
    """The app says 'production'; DORA events and every other service say 'prod'."""
    assert telemetry.normalize_env("production") == "prod"
    assert telemetry.normalize_env("development") == "dev"
    assert telemetry.normalize_env("staging") == "staging"


def test_payload_carries_the_catalog_service_name_and_bounded_tags():
    payload = telemetry.build_payload("/api/status", "ok", "prod", now=1_700_000_000)
    series = payload["series"][0]
    assert series["metric"] == "launch_planner.request"
    assert series["type"] == "count"
    assert series["points"] == [{"timestamp": 1_700_000_000, "value": 1}]
    assert set(series["tags"]) == {
        "service:launch-planner-agent",
        "env:prod",
        "endpoint:/api/status",
        "outcome:ok",
    }


def test_record_skips_health_checks(monkeypatch):
    """Fly probes /healthz every 30s. A health check is not a user."""
    monkeypatch.setenv("DD_API_KEY", "key")
    monkeypatch.setattr(telemetry, "post", lambda *a, **k: pytest.fail("posted a health check"))
    assert telemetry.record(FakeRequest("/healthz"), 200, "production") is False


def test_record_is_a_no_op_without_a_key(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.setattr(telemetry, "post", lambda *a, **k: pytest.fail("posted without a key"))
    assert telemetry.record(FakeRequest("/api/info", "/api/info"), 200, "production") is False


def test_record_sends_one_point_for_a_real_request(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setenv("DD_API_KEY", "key")
    monkeypatch.setattr(telemetry, "post", lambda payload, *a, **k: sent.append(payload) or True)

    assert telemetry.record(FakeRequest("/api/info", "/api/info"), 200, "production") is True

    tags = sent[0]["series"][0]["tags"]
    assert "endpoint:/api/info" in tags and "env:prod" in tags


def test_a_failing_send_never_raises_into_the_request(monkeypatch):
    """A telemetry outage must not become an outage of the thing it watches."""
    def boom(*_a, **_k):
        raise RuntimeError("datadog is down")

    monkeypatch.setenv("DD_API_KEY", "key")
    monkeypatch.setattr(telemetry.httpx, "post", boom)
    assert telemetry.post({}, "key") is False


def test_should_count_is_the_middleware_gate(monkeypatch):
    """The middleware asks this BEFORE spawning a thread, so Fly's 30-second
    probe costs nothing rather than costing a thread and a no-op."""
    assert telemetry.should_count(FakeRequest("/api/info", "/api/info")) is True
    assert telemetry.should_count(FakeRequest("/healthz")) is False


def test_the_app_still_serves_with_the_middleware_installed():
    """A smoke test, deliberately not asserting on the send.

    The counter is fire-and-forget on a worker thread, so whether a given point
    has left by the time the response returns is a race no test should depend
    on. What matters here is that adding it broke nothing; the decision about
    *which* requests count is asserted directly on `should_count` above.
    """
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/info").status_code == 200


def test_a_deployed_app_without_a_key_says_so(monkeypatch):
    """Zero traffic and zero credentials look identical in Datadog; only one of
    them should quietly set the entity to `experimental`."""
    monkeypatch.delenv("DD_API_KEY", raising=False)
    warning = telemetry.unconfigured_warning("production")
    assert warning is not None
    assert "fly secrets set DD_API_KEY" in warning

    monkeypatch.setenv("DD_API_KEY", "key")
    assert telemetry.unconfigured_warning("production") is None


def test_local_development_is_not_nagged(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    assert telemetry.unconfigured_warning("development") is None
