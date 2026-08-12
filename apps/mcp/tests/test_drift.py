"""The drift HTTP client: bounded, and honest about being unavailable.

Every test drives a mocked transport — nothing here touches a network. The
retry assertions matter because the failure they guard against is invisible:
an unbounded retry against a dead service turns "drift is down" into a hung
conversation.
"""

from __future__ import annotations

import httpx
import pytest
from mcp_server import drift
from mcp_server.config import McpSettings
from mcp_server.errors import DriftUnavailable


def _settings(**overrides) -> McpSettings:
    base = {
        "drift_base_url": "https://drift.test",
        "drift_run_token": None,
        "drift_timeout_seconds": 1.0,
        "drift_max_attempts": 2,
    }
    return McpSettings(**{**base, **overrides})


def _install(monkeypatch, handler) -> list[httpx.Request]:
    """Route the module's client through a mock transport; record the calls."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def fake_client(settings):
        return httpx.Client(
            base_url=settings.drift_base_url or "",
            transport=httpx.MockTransport(recording),
            headers=(
                {"Authorization": f"Bearer {settings.drift_run_token}"}
                if settings.drift_run_token
                else {}
            ),
        )

    monkeypatch.setattr(drift, "_client", fake_client)
    return seen


# --- probe: never raises ----------------------------------------------------


def test_probe_reports_not_configured_without_a_base_url():
    status = drift.probe(_settings(drift_base_url=None))
    assert status.configured is False
    assert status.reachable is False
    assert "LPA_DRIFT_BASE_URL" in status.detail


def test_probe_reports_unreachable_instead_of_raising(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    _install(monkeypatch, boom)
    status = drift.probe(_settings())
    assert status.configured is True
    assert status.reachable is False
    assert "ConnectError" in status.detail


def test_probe_reports_ok_when_healthz_answers(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, json={"status": "ok"}))
    status = drift.probe(_settings())
    assert status.reachable is True


def test_probe_treats_an_error_status_as_unreachable(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(503))
    assert drift.probe(_settings()).reachable is False


# --- get_json: raises DriftUnavailable --------------------------------------


def test_get_json_returns_the_decoded_body(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, json={"findings": []}))
    assert drift.get_json("/drift/findings", settings=_settings()) == {"findings": []}


def test_get_json_raises_when_unconfigured():
    with pytest.raises(DriftUnavailable) as caught:
        drift.get_json("/drift/findings", settings=_settings(drift_base_url=None))
    assert "not configured" in str(caught.value)


def test_get_json_raises_a_legible_error_when_unreachable(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    _install(monkeypatch, boom)
    with pytest.raises(DriftUnavailable) as caught:
        drift.get_json("/drift/findings", settings=_settings())
    rendered = str(caught.value)
    assert rendered.startswith("[drift_unavailable]")
    assert "2 attempt(s)" in rendered


def test_get_json_rejects_a_non_json_body(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(DriftUnavailable):
        drift.get_json("/drift/findings", settings=_settings())


# --- retry is bounded, and only on the right failures -----------------------


def test_transport_errors_are_retried_up_to_the_attempt_cap(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    seen = _install(monkeypatch, boom)
    drift.probe(_settings(drift_max_attempts=3))
    assert len(seen) == 3


def test_a_5xx_is_retried(monkeypatch):
    seen = _install(monkeypatch, lambda r: httpx.Response(500))
    drift.probe(_settings(drift_max_attempts=2))
    assert len(seen) == 2


def test_a_4xx_is_not_retried(monkeypatch):
    """The service answered. Retrying a 404 only makes a wrong path slow too."""
    seen = _install(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(DriftUnavailable):
        drift.get_json("/drift/findings", settings=_settings(drift_max_attempts=3))
    assert len(seen) == 1


def test_a_single_attempt_setting_means_no_retry(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    seen = _install(monkeypatch, boom)
    drift.probe(_settings(drift_max_attempts=1))
    assert len(seen) == 1


def test_the_run_token_is_sent_when_configured(monkeypatch):
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json={}))
    drift.get_json("/drift/findings", settings=_settings(drift_run_token="s3cret"))
    assert seen[0].headers["Authorization"] == "Bearer s3cret"


def test_the_client_never_reaches_the_notifying_endpoint(monkeypatch):
    """`POST /drift/run` runs a full collect-and-notify cycle. This module is
    GET-only by construction; assert it, because the whole read-only claim for
    the drift tools rests on it."""
    seen = _install(monkeypatch, lambda r: httpx.Response(200, json={}))
    drift.probe(_settings())
    drift.get_json("/drift/findings", settings=_settings())
    assert [r.method for r in seen] == ["GET", "GET"]
    assert not any("/drift/run" in str(r.url) for r in seen)
