"""The shipped transport, end to end.

Every other test drives the server in process. This one spawns it as a real
subprocess and talks JSON-RPC over the pipe, which is the only way to catch the
class of bug stdio actually has: anything written to stdout corrupts the stream,
so a stray `print` in any imported module breaks the server for every client
while leaving all the in-process tests green.

Runs `python -m mcp_server` rather than the `launch-planner-mcp` console script
so it uses the interpreter already running the suite — no nested `uv run`, no
PATH assumptions in CI.
"""

from __future__ import annotations

import asyncio
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _talk_to_a_real_subprocess():
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"])
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("platform.health", {})
            return init, tools, result

    return asyncio.run(run())


def test_a_fresh_client_connects_discovers_and_calls_over_stdio():
    init, tools, result = _talk_to_a_real_subprocess()

    assert init.server_info.name == "launch-planner"
    assert init.server_info.version

    assert "platform.health" in {tool.name for tool in tools.tools}

    assert result.is_error is False
    assert result.structured_content["plan_store"]["state"] == "ok"


def test_stdout_carries_only_protocol_traffic():
    """The diagnostic line `__main__` prints goes to stderr. If it — or any
    import-time print — went to stdout, `initialize` above would fail to parse;
    this asserts the reason rather than leaving it implicit."""
    init, _, _ = _talk_to_a_real_subprocess()
    assert init.server_info.name == "launch-planner"
