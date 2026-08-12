"""The read-only tool allowlist — the mechanism, not the documentation.

The epic's central claim is *read + simulate only in v1*. Everywhere else in
this repo that claim is structural: the deployed API has no write or LLM
endpoints, so `plan commit` and `plan jira` are simply unreachable from the
served surface. **Inside the repo that stops being true** — this process can
import those paths — so the guarantee has to be enforced instead of inherited.

Two things enforce it, and both are deliberately in this first story rather than
the last one, while the surface is one tool and they cost nothing:

1. This list, asserted against the live server in `tests/test_allowlist.py`.
   Adding a tool fails CI until someone consciously adds it here.
2. An import-linter `forbidden` contract in the root `pyproject.toml` stopping
   `mcp_server` from importing `app.cli`, `agents`, or `anthropic` at all.

They cover different things. The contract stops a *module* being reachable; the
allowlist stops a *tool* being exposed. Neither subsumes the other.

Before adding a name here, check it against the bar: the tool must not write to
the plan store, must not write to Jira, must not call an LLM, and must not send
anything. `plan.simulate` passes despite taking a scenario — `simulate()`
applies it to a deep copy and persists nothing.

The full intended surface for the epic is nine tools (RC1-243 holds the
canonical list): platform.health, plan.list, plan.get, plan.critical_path,
plan.simulate, plan.forecast, drift.check, drift.explain, status.draft. This
list grows toward that one story at a time — it is not pre-populated, because a
name here that is not registered would make the assertion vacuous.
"""

from __future__ import annotations

TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        # RC1-236 — the walking skeleton: transport, config, error mapping.
        "platform.health",
        # RC1-237 — discovery. Both read the store and schedule in memory.
        "plan.list",
        "plan.get",
        # RC1-238 — one deterministic CPM pass; selects from the payload.
        "plan.critical_path",
        # RC1-239 — applies a what-if to an in-memory copy; persists nothing.
        "plan.simulate",
        # RC1-240 — seeded sampling over the same engine; nothing is stored.
        "plan.forecast",
    }
)
