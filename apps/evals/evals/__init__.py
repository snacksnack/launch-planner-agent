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

**Deliberately not a library yet.** RC1-230 will eventually run evals in three
repos, which implies a shared package. It is not built here, and that is the
point: the harness has one consumer (the MCP server), and an interface guessed
before there are two implementations is how a harness ends up with the wrong
shape and a refactor nobody wants to do. The extraction happens in RC1-252, when
the drift digest becomes the second consumer and the seams are known rather than
imagined. Until then this is a plain workspace member with no abstraction layer:
no `Subject` protocol, no `Scorer` protocol, no plugin registry beyond a dict.
See ADR-0030.
"""

from __future__ import annotations

__version__ = "0.1.0"

from evals.case import Case  # noqa: E402
from evals.config import EvalSettings, get_eval_settings  # noqa: E402
from evals.record import (  # noqa: E402
    CaseResult,
    CharacteristicResult,
    DuplicateRunId,
    RunRecord,
    RunStore,
    SubjectVersion,
    Usage,
    new_run_id,
)

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
