"""`platform.health` — the walking skeleton.

Proves transport, config, and error mapping end to end before any real tool
exists. Reports two things a caller actually needs to know before trusting an
answer: whether the plan store is readable, and whether the drift service is
answering.

It never raises on a dead upstream. Drift being down is a *reported state*, not
a failure of the health check — a model that gets an exception here would
conclude the whole server is broken.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.config import get_settings
from app.store import SQLiteEventStore
from mcp.server import MCPServer
from pydantic import BaseModel, Field

from mcp_server import __version__
from mcp_server.drift import probe
from mcp_server.errors import legible_errors

ComponentState = Literal["ok", "unavailable", "not_configured"]


class ComponentHealth(BaseModel):
    """One dependency's state, with a sentence a model can repeat verbatim."""

    state: ComponentState
    detail: str


class PlatformHealth(BaseModel):
    """The health payload. `checked_at` is provenance: this is a point-in-time
    reading, not a subscription."""

    server_version: str
    plan_store: ComponentHealth
    drift_service: ComponentHealth
    checked_at: datetime = Field(description="UTC timestamp of this check.")


def _plan_store_health() -> ComponentHealth:
    """Read the snapshot count without creating anything.

    Deliberately does *not* open a store that isn't there. `SQLiteEventStore`
    runs its migration on construction, so probing a missing path would create
    an empty database — a write, from the one tool whose whole job is to report
    that this server does not write.
    """
    sqlite_path = get_settings().sqlite_path
    if sqlite_path != ":memory:" and not Path(sqlite_path).exists():
        return ComponentHealth(
            state="ok",
            detail=(
                f"no plan store at {sqlite_path} yet — it is created by the first "
                "`plan commit`; reads will return an empty history until then"
            ),
        )
    store = SQLiteEventStore(sqlite_path)
    try:
        count = len(store.history())
    finally:
        store.close()
    return ComponentHealth(
        state="ok", detail=f"readable at {sqlite_path} — {count} snapshot(s)"
    )


def register(server: MCPServer) -> None:
    @server.tool(
        name="platform.health",
        description=(
            "Check that the launch planner's MCP server can reach what it needs: the "
            "plan store (read-only) and the drift service. Returns a state and a "
            "one-line detail for each, plus the time of the check. Call this first "
            "when a tool has behaved oddly, or to find out whether drift data is "
            "available at all before asking for it. Takes no arguments, changes "
            "nothing, and succeeds even when a dependency is down — a dependency "
            "being unavailable is reported in the response, not raised as an error."
        ),
    )
    @legible_errors
    def platform_health() -> PlatformHealth:
        drift = probe()
        if not drift.configured:
            drift_health = ComponentHealth(state="not_configured", detail=drift.detail)
        elif not drift.reachable:
            drift_health = ComponentHealth(state="unavailable", detail=drift.detail)
        else:
            drift_health = ComponentHealth(state="ok", detail=drift.detail)

        return PlatformHealth(
            server_version=__version__,
            plan_store=_plan_store_health(),
            drift_service=drift_health,
            checked_at=datetime.now(UTC),
        )
