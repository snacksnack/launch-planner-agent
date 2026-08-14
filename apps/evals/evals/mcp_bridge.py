"""The bridge between the shipped MCP surface and the Messages API.

The subject under test is not a function — it is *what a model does when handed
this server's tool descriptions*. So this module drives the real thing: it
spawns `python -m mcp_server` as a subprocess, speaks JSON-RPC over the pipe,
and converts what comes back into Anthropic tool definitions. Same transport a
Claude Desktop user gets, same descriptions, same schemas.

Driving the shipped surface rather than importing `mcp_server.server` in process
is the point: a description that only exists in a Python constant is not what
the model reads. `apps/mcp/tests/test_stdio.py` makes the same argument for the
same reason.

**Names are translated, and that is not cosmetic.** MCP tools here are named
`plan.list`; the Messages API restricts tool names to `^[a-zA-Z0-9_-]{1,64}$`,
so a dot is rejected outright. Every MCP client that hands these tools to a
model therefore performs some version of this mapping, and the model routes on
the translated name — so `plan_list` is what is actually under test, and the
report translates back before scoring so a reader sees the shipped names.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

#: MCP allows dots in tool names; the Messages API does not. One character, one
#: direction, reversible — anything cleverer would make the report lie about
#: which tool the model actually chose.
_SEPARATOR = "."
_API_SEPARATOR = "__"


def to_api_name(mcp_name: str) -> str:
    """`plan.list` -> `plan__list`."""
    return mcp_name.replace(_SEPARATOR, _API_SEPARATOR)


def to_mcp_name(api_name: str) -> str:
    """`plan__list` -> `plan.list`. Unknown names pass through unchanged so a
    hallucinated tool name survives into the report instead of being mangled
    into something that looks real."""
    return api_name.replace(_API_SEPARATOR, _SEPARATOR)


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation the model asked for, in shipped (MCP) names."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Surface:
    """What a client sees after connecting: the tool definitions, as the
    Messages API wants them, plus the shipped names they came from."""

    tools: list[dict[str, Any]]
    mcp_names: tuple[str, ...]

    @property
    def api_names(self) -> tuple[str, ...]:
        return tuple(tool["name"] for tool in self.tools)


def _server_params(env: dict[str, str] | None = None) -> StdioServerParameters:
    """Run `python -m mcp_server` with the interpreter already running us — no
    nested `uv run`, no PATH assumptions in CI. Same choice `test_stdio.py`
    documents."""
    return StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"], env=env)


async def _with_session(env, work):
    async with (
        stdio_client(_server_params(env)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await work(session)


def discover(env: dict[str, str] | None = None) -> Surface:
    """Connect, list the tools, and render them as Messages API definitions.

    The description is passed through **verbatim**. It is the artifact under
    test — trimming or reformatting it here would mean the eval measures a
    description nobody ships.
    """

    async def work(session):
        listed = await session.list_tools()
        return [
            {
                "name": to_api_name(tool.name),
                "description": tool.description or "",
                "input_schema": tool.input_schema,
            }
            for tool in listed.tools
        ], tuple(tool.name for tool in listed.tools)

    tools, mcp_names = asyncio.run(_with_session(env, work))
    return Surface(tools=tools, mcp_names=mcp_names)


def call(name: str, arguments: dict[str, Any], env: dict[str, str] | None = None) -> str:
    """Execute one tool and return its text content.

    Only the follow-up cases need this — most routing cases never execute
    anything, because which tool was chosen is the measurement and running it
    would add latency and I/O for no signal. An error result is returned as
    text rather than raised: how the model handles a tool that failed is
    exactly what the drift-unavailable case is scoring.
    """

    # `name` is a shipped MCP name — the server knows nothing about the API's
    # charset, so it is passed through untranslated. `to_mcp_name` is a no-op on
    # a name that never went through `to_api_name`, which keeps callers honest
    # whichever form they hold.
    async def work(session):
        result = await session.call_tool(to_mcp_name(name), arguments)
        parts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
        return "\n".join(parts) or str(result.structured_content or "")

    return asyncio.run(_with_session(env, work))
