"""The one remote call in the whole server: the drift detector.

Everything else in this package reaches `planner_core` and `app` in process.
`tpm-automation-platform` is a separate service, so it gets an HTTP client —
with an explicit timeout and a bounded number of attempts, because an
interactive client should be told "unavailable" quickly rather than hang.

Two entry points, and the difference matters:

* `probe()` never raises. `platform.health` uses it, because one dead upstream
  must not make the whole server look down.
* `get_json()` raises `DriftUnavailable`. The drift tools (RC1-241) use it,
  because a drift question that cannot be answered must fail loudly rather than
  return an empty result a model would narrate as "nothing is at risk".

Only reads happen here. `POST /drift/run` runs a full collect-and-notify cycle
(Slack DMs to real people) and is deliberately unreachable from this module —
see RC1-244 for the read-only endpoint these tools are built on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from mcp_server.config import McpSettings, get_mcp_settings
from mcp_server.errors import DriftUnavailable


@dataclass(frozen=True)
class DriftStatus:
    """The result of a health probe. `reachable` is False for every failure."""

    configured: bool
    reachable: bool
    detail: str


def _client(settings: McpSettings) -> httpx.Client:
    headers = {"Accept": "application/json"}
    if settings.drift_run_token:
        headers["Authorization"] = f"Bearer {settings.drift_run_token}"
    return httpx.Client(
        base_url=settings.drift_base_url or "",
        timeout=httpx.Timeout(settings.drift_timeout_seconds),
        headers=headers,
    )


def _get(path: str, params: dict[str, Any] | None, settings: McpSettings) -> httpx.Response:
    """GET with a bounded retry on transport errors and 5xx. Raises on give-up.

    A 4xx is the service answering, so it is never retried — retrying a 404
    just makes a wrong path slow as well as wrong.
    """
    last: Exception | None = None
    with _client(settings) as client:
        for _ in range(settings.drift_max_attempts):
            try:
                response = client.get(path, params=params)
            except httpx.HTTPError as exc:  # connect, timeout, protocol
                last = exc
                continue
            if response.status_code >= 500:
                last = httpx.HTTPStatusError(
                    f"{response.status_code} from drift service",
                    request=response.request,
                    response=response,
                )
                continue
            return response
    # `drift_max_attempts` is `ge=1`, so the loop ran and either returned or set
    # `last`. Raised rather than asserted: `python -O` strips asserts, and a
    # silent `None` here would surface as an unrelated TypeError.
    raise last or httpx.HTTPError("drift request failed with no recorded cause")


def probe(settings: McpSettings | None = None) -> DriftStatus:
    """Check whether the drift service answers. Never raises."""
    settings = settings or get_mcp_settings()
    if not settings.drift_configured:
        return DriftStatus(
            configured=False,
            reachable=False,
            detail="not configured — set LPA_DRIFT_BASE_URL to enable the drift tools",
        )
    try:
        response = _get("/healthz", None, settings)
    except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
        return DriftStatus(
            configured=True,
            reachable=False,
            detail=f"unreachable at {settings.drift_base_url}: {type(exc).__name__}",
        )
    if response.status_code >= 400:
        return DriftStatus(
            configured=True,
            reachable=False,
            detail=f"{settings.drift_base_url} answered {response.status_code}",
        )
    return DriftStatus(
        configured=True, reachable=True, detail=f"reachable at {settings.drift_base_url}"
    )


def get_json(
    path: str,
    params: dict[str, Any] | None = None,
    settings: McpSettings | None = None,
) -> Any:
    """GET a read-only drift endpoint and decode it. Raises `DriftUnavailable`.

    Used by the drift tools in RC1-241; unused until that story lands, but it is
    the half of this module that defines what "unavailable" means to a caller.
    """
    settings = settings or get_mcp_settings()
    if not settings.drift_configured:
        raise DriftUnavailable(
            "The drift service is not configured (set LPA_DRIFT_BASE_URL). "
            "Every other tool is unaffected."
        )
    try:
        response = _get(path, params, settings)
    except Exception as exc:  # noqa: BLE001 — mapped to a legible tool error
        raise DriftUnavailable(
            f"Could not reach the drift service at {settings.drift_base_url} "
            f"after {settings.drift_max_attempts} attempt(s): {type(exc).__name__}."
        ) from exc
    if response.status_code >= 400:
        raise DriftUnavailable(
            f"The drift service answered {response.status_code} for {path}."
        )
    try:
        return response.json()
    except ValueError as exc:
        raise DriftUnavailable(
            f"The drift service returned a non-JSON body for {path}."
        ) from exc
