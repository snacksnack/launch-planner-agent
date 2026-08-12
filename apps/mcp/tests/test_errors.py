"""Structured errors: what a model actually reads when a tool fails."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp_server.errors import (
    DriftUnavailable,
    PlannerToolError,
    UnexpectedToolFailure,
    legible_errors,
)


def test_deliberate_errors_carry_a_stable_code():
    exc = DriftUnavailable("The drift service is not configured.")
    assert str(exc) == "[drift_unavailable] The drift service is not configured."
    assert exc.message == "The drift service is not configured."


def test_deliberate_errors_are_tool_errors():
    """They must surface as `CallToolResult(is_error=True)` — a failed call the
    model can recover from — not as a JSON-RPC protocol error, which reads as
    'the server is broken'."""
    assert issubclass(PlannerToolError, ToolError)


def test_legible_errors_passes_deliberate_failures_through_untouched():
    @legible_errors
    def tool():
        raise DriftUnavailable("upstream is down")

    with pytest.raises(DriftUnavailable) as caught:
        tool()
    assert str(caught.value) == "[drift_unavailable] upstream is down"


def test_legible_errors_relabels_anything_unexpected():
    @legible_errors
    def tool():
        {}["missing"]

    with pytest.raises(UnexpectedToolFailure) as caught:
        tool()
    rendered = str(caught.value)
    assert rendered.startswith("[internal_error] tool failed: KeyError")
    assert "Traceback" not in rendered


def test_the_original_exception_is_kept_for_debugging():
    """The model gets a clean sentence; a developer reading a transcript still
    needs the cause, so it stays on `__cause__`."""

    @legible_errors
    def tool():
        raise ValueError("the real problem")

    with pytest.raises(UnexpectedToolFailure) as caught:
        tool()
    assert isinstance(caught.value.__cause__, ValueError)
    assert str(caught.value.__cause__) == "the real problem"


def test_legible_errors_preserves_the_signature():
    """The SDK builds each tool's input schema from the wrapped function's
    signature. A decorator that hid it would silently produce an empty schema."""
    import inspect

    @legible_errors
    def tool(plan_ref: str, days: int = 5) -> str:
        return f"{plan_ref}/{days}"

    params = inspect.signature(tool).parameters
    assert list(params) == ["plan_ref", "days"]
    assert params["days"].default == 5
    assert tool("p", 1) == "p/1"
