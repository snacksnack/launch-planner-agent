"""mcp_server — the MCP layer of the launch planner.

Exposes the delivery-intelligence platform as MCP tools so any MCP client can
drive it in conversation. The plan tools call `planner_core` and the `app` layer
**in process** — the same code path the CLI uses, so parity with `plan <verb>`
is structural rather than something two implementations have to agree on. Only
the drift tools cross the network, to `tpm-automation-platform`.

This is the top layer: it may import `app` and `planner_core`; none of them may
import it. Enforced in CI by import-linter, alongside a `forbidden` contract
keeping it out of `app.cli`, `agents`, and `anthropic` — the write and LLM
paths. See `allowlist.py` for why that is enforced rather than assumed.

The distribution is `launch-planner-mcp` and lives at `apps/mcp/`; the
importable package is `mcp_server` because `mcp` belongs to the official SDK.
"""

# Declared before the submodule imports below: `tools.health` reads it at import
# time, and this module is partially initialised while that import runs.
__version__ = "0.1.1"

from mcp_server.allowlist import TOOL_ALLOWLIST  # noqa: E402
from mcp_server.config import McpSettings, get_mcp_settings  # noqa: E402
from mcp_server.errors import (  # noqa: E402
    DriftUnavailable,
    PlannerToolError,
    UnexpectedToolFailure,
    legible_errors,
)
from mcp_server.server import build_server  # noqa: E402

__all__ = [
    "__version__",
    "TOOL_ALLOWLIST",
    "McpSettings",
    "get_mcp_settings",
    "DriftUnavailable",
    "PlannerToolError",
    "UnexpectedToolFailure",
    "legible_errors",
    "build_server",
]
