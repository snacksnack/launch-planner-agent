"""evals — the eval harness for the launch planner's LLM systems.

Answers the question every other layer asserts rather than proves: *how do you
know the output is any good?* A subject is run against frozen cases, its output
is scored on named characteristics, and the result is appended to a run log
carrying the subject version, the token cost, and the latency — so a regression
can be attributed to a model or prompt change rather than guessed at.

This is the top layer. It may import `mcp_server`, `app`, `agents`, and
`planner_core`; none of them may import it. Enforced in CI by import-linter,
which also keeps `evals` out of `planner_core`'s reachable set — a measurement
tool must never become a dependency of the thing it measures.

**The reusable half now lives in `agent_evals`** (RC1-261). `Case`, run records,
the deterministic groundedness checker, the rubric and the calibration machinery
were extracted once the drift digest became a second consumer — the sequencing
ADR-0030 argued for, where the seams get read off two implementations instead of
guessed from one. What remains here is everything that is *about this repo*: the
subjects, the fact-set generator, the MCP bridge, the CLI, the config, and the
measured cost ceilings.

The re-exports below are kept so `from evals import Case` still resolves. Which
half of the harness a name lives in is a packaging fact, not something every
subject should have to track.
"""

from __future__ import annotations

__version__ = "0.2.0"

from agent_evals.case import Case  # noqa: E402
from agent_evals.record import (  # noqa: E402
    CaseResult,
    CharacteristicResult,
    DuplicateRunId,
    RunRecord,
    RunStore,
    SubjectVersion,
    Usage,
    new_run_id,
)

from evals.config import EvalSettings, get_eval_settings  # noqa: E402

__all__ = [
    "__version__",
    "Case",
    "CaseResult",
    "CharacteristicResult",
    "DuplicateRunId",
    "EvalSettings",
    "RunRecord",
    "RunStore",
    "SubjectVersion",
    "Usage",
    "get_eval_settings",
    "new_run_id",
]
