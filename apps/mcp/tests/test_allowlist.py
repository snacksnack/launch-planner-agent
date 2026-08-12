"""The read-only allowlist (RC1-236).

This is the mechanism behind the epic's "read + simulate only in v1" claim, so
it is a real gate rather than documentation: adding a tool fails here until
someone consciously adds its name to `TOOL_ALLOWLIST`, at which point they have
had to think about whether it writes anything.
"""

from __future__ import annotations

import asyncio

from mcp_server.allowlist import TOOL_ALLOWLIST
from mcp_server.server import build_server


def _registered() -> set[str]:
    server = build_server()
    return {tool.name for tool in asyncio.run(server.list_tools())}


def test_registered_tools_exactly_match_the_allowlist():
    assert _registered() == set(TOOL_ALLOWLIST)


def test_an_unlisted_tool_fails_the_gate():
    """The guard itself: registering off-list must be detectable.

    Without this, a regression that made the comparison vacuous (an empty
    allowlist matching an empty registry, say) would pass silently.
    """
    server = build_server()

    @server.tool(name="plan.commit", description="A write tool nobody approved.")
    def rogue() -> str:
        return "should never be reachable"

    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    assert registered != set(TOOL_ALLOWLIST)
    assert registered - set(TOOL_ALLOWLIST) == {"plan.commit"}


def test_allowlist_contains_no_write_shaped_names():
    """A cheap smell test on the list itself.

    It cannot prove a tool is read-only — that is the import contract's job, and
    the mock sweep in RC1-243 — but a name like `plan.commit` appearing here
    should never get as far as review.
    """
    forbidden_verbs = ("commit", "delete", "save", "push", "send", "write", "run")
    for name in TOOL_ALLOWLIST:
        verb = name.split(".")[-1]
        assert verb not in forbidden_verbs, f"{name} looks like a write tool"
