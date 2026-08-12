"""Structured, model-legible errors.

Anything raised out of a tool body is caught by the SDK and turned into
``CallToolResult(is_error=True)`` whose text is ``str(exc)``. That is the only
thing the model ever sees, so the whole job here is to guarantee that string is
a sentence a model can act on rather than a repr of whatever blew up.

Every deliberate failure carries a stable ``code`` in square brackets, so a
caller can branch on the code and a reader can grep for it:

    Error executing tool drift.check: [drift_unavailable] The drift service is
    not configured (set LPA_DRIFT_BASE_URL). Other tools are unaffected.

`MCPError` is deliberately *not* used: it surfaces as a top-level JSON-RPC
protocol error, which reads to a model as "the server is broken" rather than
"this call failed, try something else."
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, ClassVar

from mcp.server.mcpserver.exceptions import ToolError


class PlannerToolError(ToolError):
    """A failure this server raised on purpose, with a stable code.

    Subclasses set `code`; the rendered message is always ``[code] message``.
    """

    code: ClassVar[str] = "planner_error"

    def __init__(self, message: str) -> None:
        super().__init__(f"[{self.code}] {message}")
        self.message = message


class DriftUnavailable(PlannerToolError):
    """The drift service is unconfigured, unreachable, or returned an error.

    Raised by the drift tools (RC1-241). `platform.health` deliberately does not
    raise this — it reports drift as unavailable and stays healthy, because one
    dead upstream must not make the whole server look down.
    """

    code = "drift_unavailable"


class PlanNotFound(PlannerToolError):
    """The requested plan reference matched nothing.

    Always names what *would* work — a model that gets "not found" with no
    alternatives will guess another format rather than call `plan.list`.
    """

    code = "plan_not_found"


class AmbiguousPlanRef(PlannerToolError):
    """A hash prefix matched more than one snapshot.

    Deliberately an error rather than "pick the newest": silently choosing would
    make the model confidently report a date computed from a plan the user did
    not ask about, and nothing downstream would reveal it.
    """

    code = "ambiguous_plan_ref"

    def __init__(self, ref: str, candidates: list[str]) -> None:
        listed = ", ".join(candidates)
        super().__init__(
            f"The reference {ref!r} matches {len(candidates)} snapshots ({listed}). "
            "Use more characters of the hash, or the version number."
        )
        self.candidates = candidates


class InvalidArgument(PlannerToolError):
    """An argument was malformed. Distinct from `plan_not_found`: the caller
    passed something unusable, rather than naming something that isn't there."""

    code = "invalid_argument"


class UnexpectedToolFailure(PlannerToolError):
    """A bug: something the tool did not anticipate. Never a raw traceback."""

    code = "internal_error"


def legible_errors[F: Callable[..., Any]](fn: F) -> F:
    """Wrap a tool body so nothing escapes as an unstructured exception.

    Deliberate failures pass through untouched. Anything else is relabelled with
    its exception class and message — enough for a human to debug from a
    transcript, without a traceback the model would try to interpret.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise  # already legible — ours, or the SDK's own
        except Exception as exc:  # noqa: BLE001 — the point is to catch everything
            raise UnexpectedToolFailure(
                f"{fn.__name__} failed: {type(exc).__name__}: {exc}"
            ) from exc

    return wrapper  # type: ignore[return-value]
