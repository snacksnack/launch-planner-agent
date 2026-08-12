"""A real MCP client against the real server.

`Client(server)` runs the actual protocol — initialize, list_tools, call_tool —
over the SDK's in-memory transport, so this covers the wire contract rather than
just the Python functions. The stdio transport itself is exercised by
`test_stdio.py`.
"""

from __future__ import annotations

import asyncio
import json

from mcp.client.client import Client
from mcp_server.server import build_server


def _round_trip(coro_fn):
    async def run():
        async with Client(build_server()) as client:
            return await coro_fn(client)

    return asyncio.run(run())


def test_a_fresh_client_discovers_the_health_tool():
    tools = _round_trip(lambda c: c.list_tools())
    names = {tool.name for tool in tools.tools}
    assert "platform.health" in names


def test_tool_description_is_substantive_enough_to_route_on():
    """Descriptions are a prompt surface, not documentation (see RC1-243).

    A one-liner is the failure mode that makes a model pick the wrong tool, so
    hold a floor on length and require the description to say what it does not
    do — the read-only claim is the part that must survive a rewrite.
    """
    tools = _round_trip(lambda c: c.list_tools())
    health = next(t for t in tools.tools if t.name == "platform.health")
    assert health.description is not None
    assert len(health.description) > 200
    assert "changes nothing" in health.description


def test_server_instructions_state_the_read_only_boundary():
    async def run():
        async with Client(build_server()) as client:
            return client.instructions

    instructions = asyncio.run(run())
    assert instructions is not None
    assert "read-only" in instructions.lower()


def test_calling_health_returns_structured_content():
    result = _round_trip(lambda c: c.call_tool("platform.health", {}))
    assert result.is_error is False
    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["plan_store"]["state"] == "ok"
    assert payload["drift_service"]["state"] == "not_configured"
    assert payload["checked_at"]

    # The text block must carry the same facts — a client that ignores
    # structured content still has to get a usable answer.
    text = json.loads(result.content[0].text)
    assert text["plan_store"]["state"] == "ok"


def test_unknown_tool_is_an_error_not_a_crash():
    result = _round_trip(lambda c: c.call_tool("plan.commit", {}))
    assert result.is_error is True
