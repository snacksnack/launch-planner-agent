"""Entrypoint: serve over stdio.

    uv run launch-planner-mcp

stdio only in v1 — local clients, no hosting, no auth surface. The client spawns
this process and talks to it over the pipe, so **stdout belongs to the protocol**:
anything printed there corrupts the stream. Diagnostics go to stderr.
"""

from __future__ import annotations

import sys

from mcp_server.server import build_server


def main() -> int:
    server = build_server()
    print(f"{server.name}: MCP server ready on stdio", file=sys.stderr)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
