"""Request counts to Datadog, posted straight to the metrics API (RC1-455).

This service is the one deployed thing in the estate that emits nothing, so
nobody can say whether it serves anyone. It deploys about twice a week anyway.

The obvious fix does not work here. `LLMObs.enable()` is how every other
service in this estate ends up with telemetry, but it traces *model calls*, and
this API deliberately makes none — `main.py` builds its narrative from
`fallback_narrative`, and the LLM path lives in the gated `plan status` CLI. And
ordinary APM tracing needs a Datadog Agent, which this estate does not run. So
the remaining agentless option is to count requests and POST them, the same way
the platform's security-posture collector posts its gauges.

What this answers is deliberately narrow: **is anyone calling this, and does it
work when they do.** Not latency, not traces.

`/healthz` is excluded on purpose. Fly probes it every 30 seconds; counting
that would report 2,880 "requests" a day and answer the wrong question
entirely. A health check is not a user.

Cardinality is bounded by construction: twelve non-health routes times three
outcomes is the ceiling, and the `endpoint` tag is the route *template*
(`/api/scenarios/{name}`), never the resolved path, so a scenario per visitor
cannot turn into a series per visitor.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

#: Singular, per the estate's metric-naming convention.
METRIC = "launch_planner.request"

#: Fly probes this every 30s. A health check is not a user.
EXCLUDED_PATHS = frozenset({"/healthz"})

#: Short: this runs inside a request, and telemetry must never hold one up.
TIMEOUT_S = 2.0

#: `/api/v2/series` takes the metric type as an integer enum, NOT a string:
#: 0 unspecified, 1 count, 2 rate, 3 gauge. Sending "count" is rejected with
#: `unknown value "count" for enum datadoghq.api.series.v2.MetricType`, and the
#: first cut of this module did exactly that — every point 400'd for half an
#: hour while the swallowed error made it look like no traffic.
COUNT = 1

#: This app calls its own environment "production"; the rest of the estate tags
#: `env:prod` — the DORA deploy events, and DD_ENV on the other services. One
#: env filter should find all of them, so normalize on the way out rather than
#: renaming a setting the app uses for other things.
_ENV_ALIASES = {"production": "prod", "development": "dev"}


def normalize_env(env: str) -> str:
    return _ENV_ALIASES.get(env, env)


def unconfigured_warning(env: str) -> str | None:
    """A warning when this is deployed but cannot send, else None.

    Without `DD_API_KEY` every `record` is a silent no-op, and silence here is
    indistinguishable from "nobody calls this service" — which is the exact
    question the counter exists to answer. A wrong answer would then be read as
    evidence and set the catalog entity to `experimental`. So say so at boot:
    the estate's recurring lesson is that a skipped thing looks like a passing
    one unless it announces itself.
    """
    if normalize_env(env) == "dev" or os.environ.get("DD_API_KEY"):
        return None
    return (
        "telemetry: DD_API_KEY is not set, so launch_planner.request will never "
        "be sent. Zero traffic and zero credentials look the same in Datadog. "
        "Set it with: fly secrets set DD_API_KEY=... -a launch-planner-agent"
    )


def should_count(request: Any) -> bool:
    """Whether this request is worth a point.

    Consulted by the middleware *before* it spawns a thread, so Fly's 30-second
    probe costs nothing at all rather than costing a thread and a no-op.
    `record` re-checks, because a guard that only one caller honours is not a
    guard.
    """
    path = request.url.path if hasattr(request, "url") else ""
    return path not in EXCLUDED_PATHS


def outcome_for(status_code: int) -> str:
    """Three buckets, not one series per status code."""
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "ok"


def route_template(request: Any, fallback: str) -> str:
    """The matched route's template, so path parameters cannot inflate tags.

    `/api/scenarios/mine` and `/api/scenarios/yours` are both
    `/api/scenarios/{name}`. An unmatched path (a 404 for a URL no route
    claims) has no template and would otherwise be unbounded, so it is folded
    into a single `unmatched` bucket.
    """
    route = request.scope.get("route") if hasattr(request, "scope") else None
    path = getattr(route, "path", None)
    if path:
        return str(path)
    return "unmatched" if fallback not in EXCLUDED_PATHS else fallback


def build_payload(endpoint: str, outcome: str, env: str, now: float | None = None) -> dict:
    """The series document for one counted request."""
    return {
        "series": [
            {
                "metric": METRIC,
                "type": COUNT,
                "points": [{"timestamp": int(now or time.time()), "value": 1}],
                "tags": [
                    # The service name is the Software Catalog entity and the
                    # name the Fly Deploy workflow reports to DORA (RC1-447).
                    "service:launch-planner-agent",
                    f"env:{env}",
                    f"endpoint:{endpoint}",
                    f"outcome:{outcome}",
                ],
            }
        ]
    }


def post(payload: dict, api_key: str, site: str = "datadoghq.com") -> bool:
    """Send one series document. Returns whether Datadog accepted it.

    Every failure is swallowed — a telemetry outage must never become an outage
    of the thing it is watching, and this runs in the request path — but it is
    swallowed *loudly*. A rejected point and an unvisited service look identical
    in Datadog, and that silence is exactly what this metric exists to
    distinguish. The first cut logged nothing and spent half an hour looking
    like no traffic while every send 400'd on the type enum.
    """
    try:
        resp = httpx.post(
            f"https://api.{site}/api/v2/series",
            json=payload,
            headers={"DD-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=TIMEOUT_S,
        )
        if resp.status_code >= 300:
            print(
                f"telemetry: Datadog rejected a point, HTTP {resp.status_code}: "
                f"{resp.text[:200]}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - see docstring; never raise into a request
        print(f"telemetry: could not send a point: {exc!r}", file=sys.stderr)
        return False


def record(request: Any, status_code: int, env: str) -> bool:
    """Count one request, unless it is a health check or there is no API key.

    Returns whether a point was sent, which is what the tests assert on.
    """
    path = request.url.path if hasattr(request, "url") else ""
    if path in EXCLUDED_PATHS:
        return False
    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return False
    payload = build_payload(
        endpoint=route_template(request, path),
        outcome=outcome_for(status_code),
        env=normalize_env(env),
    )
    return post(payload, api_key, os.environ.get("DD_SITE", "datadoghq.com"))
