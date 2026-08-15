"""What a call cost, exposed without changing what an agent returns.

Every agent here funnels through `client.messages.parse(...)` and returns only
`response.parsed_output`, which discards the token counts. That was invisible
until an eval reported **$0 for a subject that had just spent 39 seconds against
a real model** — a budget story with wrong numbers is worse than no budget story
(RC1-254).

`run()` still returns exactly what it always returned; the counts land on
`agent.last_usage` as a side channel. That keeps the CLI and MCP callers
untouched — they neither know nor care — while the eval harness can read the
real cost.

**One agent instance, one call.** `last_usage` is per-instance mutable state, so
a shared agent driven concurrently would report whichever call finished last.
Every caller here constructs an agent per unit of work, and the evals construct
one per case; anything that stops being true needs a different mechanism rather
than a lock.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentUsage:
    """Tokens for one agent call, and the model that consumed them.

    The model is carried alongside because cost is meaningless without it —
    the same token counts price differently per model, and the drift digest
    already runs a different one from the planner.
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def of(cls, response: object, model: str) -> AgentUsage | None:
        """Read usage off a response, or `None` if it carries none.

        `None` rather than zeros: a fake client in a test has no usage, and
        reporting that as "this call was free" is the exact confusion this
        module exists to remove.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        return cls(
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
