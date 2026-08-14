"""The bridge, against the real shipped server — no model, no credentials.

`test_tool_selection.py` fakes the surface to stay fast; this one spawns
`python -m mcp_server` for real, because the thing worth catching here is a
mismatch between what the server ships and what a Messages API client can
actually be handed. Same argument `apps/mcp/tests/test_stdio.py` makes about
in-process tests.
"""

from __future__ import annotations

import re

from evals.mcp_bridge import call, discover, to_api_name

#: What the Messages API accepts as a tool name. A dot is not in it.
_API_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def test_every_shipped_tool_survives_translation():
    """The load-bearing check: MCP names here contain dots, and a dot is
    outside the Messages API's permitted tool-name charset. If this fails, the
    whole surface is unusable from a model — silently, as a 400 per request."""
    surface = discover()

    assert surface.mcp_names, "the server exposed no tools"
    for name in surface.api_names:
        assert _API_TOOL_NAME.match(name), f"{name!r} is not a valid Messages API tool name"


def test_the_surface_is_the_shipped_nine():
    surface = discover()
    assert set(surface.mcp_names) == {
        "platform.health",
        "plan.list",
        "plan.get",
        "plan.critical_path",
        "plan.simulate",
        "plan.forecast",
        "status.draft",
        "drift.check",
        "drift.explain",
    }


def test_descriptions_are_passed_through_verbatim():
    """The description is the artifact under test — reformatting it here would
    mean the eval measures prose nobody ships."""
    surface = discover()
    by_name = {tool["name"]: tool for tool in surface.tools}
    health = by_name[to_api_name("platform.health")]

    assert len(health["description"]) > 200, "description looks truncated"
    assert "plan store" in health["description"]
    assert health["input_schema"]["properties"] == {}


def test_a_tool_can_be_executed_over_the_same_transport():
    """The follow-up cases need real tool output, not a stub."""
    output = call("platform.health", {})
    assert "server_version" in output or "plan_store" in output


def test_drift_reports_unavailable_when_it_is_not_configured():
    """The precondition the drift-unavailable case rests on. If this ever
    starts returning findings, that case is silently measuring nothing."""
    output = call("drift.check", {}, env=_no_drift())
    assert "drift" in output.lower()
    assert "unavailable" in output.lower() or "not configured" in output.lower()


def _no_drift() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.update({"LPA_DRIFT_BASE_URL": "", "LPA_DRIFT_RUN_TOKEN": ""})
    return env
