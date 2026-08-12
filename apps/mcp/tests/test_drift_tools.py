"""`drift.check` and `drift.explain`.

The upstream service is mocked here, but the shapes are the ones RC1-244
actually returns — captured from the live endpoint rather than written from
memory, because a wrapper that agrees with an imagined contract is worse than
no test at all.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp.client.client import Client
from mcp_server import drift as drift_client
from mcp_server.errors import InvalidArgument
from mcp_server.server import build_server
from mcp_server.tools.drift import decode_finding_id, encode_finding_id

# Verbatim from GET /drift/findings on the seeded database (RC1-244).
RED = {
    "rule_type": "timeline_inversion",
    "upstream": "RC1-157",
    "downstream": "RC1-158",
    "severity": 48.0,
    "severity_bucket": "red",
    "detail": "RC1-157 due 2026-07-20 lands after RC1-158 start/due 2026-07-08 (12d overlap).",
    "first_seen_run": 1,
    "is_new": True,
}
YELLOW = {
    "rule_type": "lead_time_risk",
    "upstream": "RC1-161",
    "downstream": "RC1-162",
    "severity": 12.0,
    "severity_bucket": "yellow",
    "detail": "RC1-161 not started; RC1-162 starts 2026-07-04.",
    "first_seen_run": 1,
    "is_new": False,
}
NO_UPSTREAM = {
    "rule_type": "orphan_risk",
    "upstream": None,
    "downstream": "RC1-170",
    "severity": 2.0,
    "severity_bucket": "white",
    "detail": "RC1-170 has no upstream link.",
    "first_seen_run": 3,
    "is_new": True,
}


def _body(findings: list[dict], run_id: int | None = 1) -> dict:
    return {
        "project_key": "RC1",
        "run_id": run_id,
        "run_at": "2026-07-02T20:37:51.082875Z" if run_id else None,
        "count": len(findings),
        "findings": findings,
    }


@pytest.fixture
def upstream(monkeypatch):
    """Route the drift client at a mock transport; record every request."""
    seen: list[httpx.Request] = []
    routes: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for path, response in routes.items():
            if request.url.path == path:
                return response
        return httpx.Response(404, json={"detail": "not found"})

    def fake_client(settings):
        return httpx.Client(
            base_url="https://drift.test", transport=httpx.MockTransport(handler)
        )

    monkeypatch.setenv("LPA_DRIFT_BASE_URL", "https://drift.test")
    drift_client.get_mcp_settings.cache_clear()
    monkeypatch.setattr(drift_client, "_client", fake_client)

    class Upstream:
        requests = seen

        def serve(self, path: str, payload: dict, status: int = 200) -> None:
            routes[path] = httpx.Response(status, json=payload)

    yield Upstream()
    drift_client.get_mcp_settings.cache_clear()


def _call(tool: str, args: dict | None = None):
    async def run():
        async with Client(build_server()) as client:
            return await client.call_tool(tool, args or {})

    return asyncio.run(run())


def _ok(tool: str, args: dict | None = None) -> dict:
    result = _call(tool, args)
    assert result.is_error is False, result.content
    return result.structured_content


# --- finding ids ------------------------------------------------------------


def test_a_finding_id_round_trips():
    for rule, up, down in (
        ("timeline_inversion", "RC1-157", "RC1-158"),
        ("orphan_risk", None, "RC1-170"),
    ):
        assert decode_finding_id(encode_finding_id(rule, up, down)) == (rule, up, down)


def test_ids_are_url_safe():
    """They travel in a path segment on the way back out."""
    encoded = encode_finding_id("timeline_inversion", "RC1-157", "RC1-158")
    assert "/" not in encoded and "+" not in encoded and "=" not in encoded


@pytest.mark.parametrize("bad", ["", "not-base64!!", "YWJj", "  "])
def test_a_malformed_id_is_rejected_before_the_service_is_called(bad, upstream):
    """A composed id would otherwise surface as a bare 404 from the service,
    which reads as 'the finding cleared' rather than 'that was never valid'."""
    result = _call("drift.explain", {"finding_id": bad})
    assert result.is_error is True
    assert "[invalid_argument]" in result.content[0].text
    assert upstream.requests == []


def test_decode_raises_invalid_argument_directly():
    with pytest.raises(InvalidArgument):
        decode_finding_id("nonsense!")


# --- drift.check ------------------------------------------------------------


def test_findings_are_returned_with_ids_and_evidence(upstream):
    upstream.serve("/drift/findings", _body([RED, YELLOW]))
    payload = _ok("drift.check")

    assert payload["count"] == 2
    first = payload["findings"][0]
    assert first["rule_type"] == "timeline_inversion"
    assert first["detail"] == RED["detail"]
    assert decode_finding_id(first["finding_id"]) == ("timeline_inversion", "RC1-157", "RC1-158")


def test_the_run_is_reported_and_never_claimed_to_be_live(upstream):
    """These are the last scheduled run's findings. A response that omitted that
    invites a reader to treat stored data as a fresh scan."""
    upstream.serve("/drift/findings", _body([RED]))
    payload = _ok("drift.check")

    assert payload["run_id"] == 1
    assert payload["run_at"].startswith("2026-07-02")
    assert payload["is_live"] is False


def test_buckets_are_counted(upstream):
    upstream.serve("/drift/findings", _body([RED, YELLOW, NO_UPSTREAM]))
    counts = _ok("drift.check")["counts_by_bucket"]
    assert counts == {"red": 1, "yellow": 1, "white": 1}


def test_filters_are_passed_through(upstream):
    upstream.serve("/drift/findings", _body([RED]))
    _ok("drift.check", {"bucket": "red", "rule": "timeline_inversion", "since_run": 4})

    params = upstream.requests[-1].url.params
    assert params["bucket"] == "red"
    assert params["rule"] == "timeline_inversion"
    assert params["since_run"] == "4"


def test_an_invalid_bucket_is_rejected_locally(upstream):
    result = _call("drift.check", {"bucket": "purple"})
    assert result.is_error is True
    assert "[invalid_argument]" in result.content[0].text
    assert upstream.requests == []


def test_no_run_yet_is_distinguished_from_no_drift(upstream):
    """'The detector has never run' and 'the detector found nothing' both give
    an empty list and mean opposite things."""
    upstream.serve("/drift/findings", _body([], run_id=None))
    payload = _ok("drift.check")

    assert payload["run_id"] is None
    assert payload["count"] == 0
    assert "not the same as" in payload["note"]


def test_an_empty_run_says_so_plainly(upstream):
    upstream.serve("/drift/findings", _body([]))
    assert _ok("drift.check")["note"] == "The last run found no drift."


def test_an_empty_filtered_result_says_it_was_filtered(upstream):
    upstream.serve("/drift/findings", _body([]))
    note = _ok("drift.check", {"bucket": "red"})["note"]
    assert "matched those filters" in note


# --- drift.explain ----------------------------------------------------------


def test_explain_addresses_the_finding_by_its_identity(upstream):
    upstream.serve("/drift/findings/timeline_inversion/RC1-158", _body([RED]))
    finding_id = encode_finding_id("timeline_inversion", "RC1-157", "RC1-158")

    payload = _ok("drift.explain", {"finding_id": finding_id})
    assert payload["findings"][0]["detail"] == RED["detail"]

    request = upstream.requests[-1]
    assert request.url.path == "/drift/findings/timeline_inversion/RC1-158"
    assert request.url.params["upstream"] == "RC1-157"


def test_explain_omits_upstream_for_rules_that_have_none(upstream):
    upstream.serve("/drift/findings/orphan_risk/RC1-170", _body([NO_UPSTREAM]))
    _ok("drift.explain", {"finding_id": encode_finding_id("orphan_risk", None, "RC1-170")})
    assert "upstream" not in upstream.requests[-1].url.params


def test_an_id_from_check_is_accepted_by_explain_unchanged(upstream):
    """The contract that matters: a model reads a list and hands one back."""
    upstream.serve("/drift/findings", _body([RED]))
    finding_id = _ok("drift.check")["findings"][0]["finding_id"]

    upstream.serve("/drift/findings/timeline_inversion/RC1-158", _body([RED]))
    assert _ok("drift.explain", {"finding_id": finding_id})["count"] == 1


def test_a_cleared_finding_is_an_error_not_stale_evidence(upstream):
    """The service 404s when a finding is no longer open."""
    result = _call(
        "drift.explain",
        {"finding_id": encode_finding_id("timeline_inversion", "RC1-157", "RC1-999")},
    )
    assert result.is_error is True
    assert "[drift_unavailable]" in result.content[0].text


# --- unavailability ---------------------------------------------------------


def test_an_unconfigured_service_fails_loudly_rather_than_returning_nothing(monkeypatch):
    """An empty result would be narrated as 'nothing is at risk', which is a
    wrong answer rather than a missing one."""
    monkeypatch.setenv("LPA_DRIFT_BASE_URL", "")
    drift_client.get_mcp_settings.cache_clear()
    try:
        result = _call("drift.check")
        assert result.is_error is True
        text = result.content[0].text
        assert "[drift_unavailable]" in text
        assert "LPA_DRIFT_BASE_URL" in text
    finally:
        drift_client.get_mcp_settings.cache_clear()


def test_an_unreachable_service_is_reported_as_unavailable(monkeypatch, upstream):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        drift_client,
        "_client",
        lambda s: httpx.Client(
            base_url="https://drift.test", transport=httpx.MockTransport(boom)
        ),
    )
    result = _call("drift.check")
    assert result.is_error is True
    assert "[drift_unavailable]" in result.content[0].text


def test_the_rest_of_the_server_still_works_when_drift_is_down(monkeypatch):
    """One dead upstream must not take the planner tools with it."""
    monkeypatch.setenv("LPA_DRIFT_BASE_URL", "")
    drift_client.get_mcp_settings.cache_clear()
    try:
        assert _call("plan.get").is_error is False
        assert _call("platform.health").is_error is False
    finally:
        drift_client.get_mcp_settings.cache_clear()


# --- the scan endpoint is unreachable ---------------------------------------


def test_no_tool_can_trigger_a_scan(upstream):
    """`POST /drift/run` collects from Jira, calls Anthropic, and DMs real
    people on Slack. Nothing here may reach it, by any path."""
    upstream.serve("/drift/findings", _body([RED]))
    upstream.serve("/drift/findings/timeline_inversion/RC1-158", _body([RED]))

    _ok("drift.check")
    _ok(
        "drift.explain",
        {"finding_id": encode_finding_id("timeline_inversion", "RC1-157", "RC1-158")},
    )

    assert upstream.requests, "the mock transport was never exercised"
    for request in upstream.requests:
        assert request.method == "GET"
        assert "/drift/run" not in str(request.url)


def test_repeated_checks_stay_read_only(upstream):
    upstream.serve("/drift/findings", _body([RED]))
    for _ in range(10):
        _ok("drift.check")
    assert all(r.method == "GET" for r in upstream.requests)
    assert len(upstream.requests) == 10


# --- shape ------------------------------------------------------------------


def test_the_descriptions_say_the_data_is_not_live():
    async def run():
        async with Client(build_server()) as client:
            return await client.list_tools()

    tools = {t.name: t.description for t in asyncio.run(run()).tools}
    assert "LAST SCHEDULED RUN" in tools["drift.check"]
    assert "not a fresh scan" in tools["drift.check"]
    assert "Do not compose one" in tools["drift.explain"]


def test_the_response_is_bounded(upstream):
    upstream.serve("/drift/findings", _body([RED, YELLOW, NO_UPSTREAM] * 20))
    assert len(json.dumps(_ok("drift.check"))) < 30_000
