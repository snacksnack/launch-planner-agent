"""Tool registration.

Each module here owns one tool and exposes a `register(server)` that binds it.
Adding a tool means adding its registrar below **and** its name to
`mcp_server.allowlist.TOOL_ALLOWLIST` — the allowlist test fails until both
happen, which is the point.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from mcp.server import MCPServer

from mcp_server.tools import critical_path, forecast, health, plans, simulate

REGISTRARS: Sequence[Callable[[MCPServer], None]] = (
    health.register,
    plans.register,
    critical_path.register,
    simulate.register,
    forecast.register,
)


def register_all(server: MCPServer) -> None:
    for register in REGISTRARS:
        register(server)


__all__ = ["REGISTRARS", "register_all"]
