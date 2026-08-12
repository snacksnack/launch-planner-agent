"""Assemble the MCP server.

Kept separate from `__main__` so tests can build a server and inspect its tools
without starting a transport.
"""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_server.tools import register_all

INSTRUCTIONS = """\
Tools for the launch planner: a deterministic critical-path engine over a
committed plan of record, plus a drift detector watching it at runtime.

This server is read-only. Nothing here writes to Jira, mutates a committed plan,
or sends a message. `plan.simulate` applies a what-if to a copy in memory and
persists nothing. Committing a plan, generating Jira tickets, and running the
LLM agents are CLI-only actions behind a human approval gate, and are not
reachable from any tool here.

Numbers come from the same engine the CLI and dashboard use, so a figure you
report here will match `uv run plan <verb>` for the same inputs. Every response
says which plan it read and when it was computed — quote that alongside any date
you give, because a plan of record moves.\
"""


def build_server(name: str = "launch-planner") -> MCPServer:
    """Return a fully-registered server. Does not start a transport."""
    from mcp_server import __version__  # deferred: this module loads during its init

    server = MCPServer(
        name=name,
        title="Launch Planner",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    register_all(server)
    return server
