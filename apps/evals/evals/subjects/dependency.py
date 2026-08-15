"""Dependency goldens — and the repair count is the real signal (RC1-257).

RC1-257 asks for a cycle check on every `dependency` output, on the grounds that
a cycle is the failure that most obviously invalidates a plan. Running it against
the shipped agent showed the check needs stating more carefully than that.

## The output is acyclic by construction

`DependencyAgent.run` does structural triage and then calls `resolve_cycles`
before returning, so a cycle in the *result* is impossible unless that repair
itself regresses. Asserting "no cycles in the output" therefore measures the
repair function, not the model — it would pass no matter how badly the model
behaved, which is precisely the shape of check that makes a suite look green for
the wrong reason.

So the acyclicity check is kept, and honestly labelled: it is a **regression
guard on `resolve_cycles`**, cheap and worth having, but it is not evidence about
the agent.

The evidence about the agent is what the repair had to *do*:

* **`rejections`** — edges naming tasks that do not exist. The direct analogue of
  an invented ticket key, and the agent's own triage already counts them.
* **`cycle_breaks`** — edges removed to make the graph schedulable. A model
  proposing circular precedence is degrading even though the plan survives.

Both are gated at zero on the flagship, where the task list is given and there is
no excuse for either. That is the check RC1-257 was reaching for.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import networkx as nx
from agent_evals.case import Case
from agent_evals.pricing import cost_usd
from agent_evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage
from agents.dependency import DEFAULT_MODEL, SYSTEM_PROMPT, DependencyAgent, DependencyResult
from app.config import get_settings
from planner_core import Constraint, Task

from evals import planning

NAME = "dependency"

#: Precedence the flagship PRD states outright: you cannot cut over before the
#: data is migrated, and you cannot decommission before you have cut over.
#: Expressed as task-name substrings rather than golden ids, because the ids
#: come from whatever the breakdown proposed on this run.
_REQUIRED_ORDER: dict[str, tuple[tuple[str, str], ...]] = {
    "jira-cloud-migration": (("migrat", "cutover"), ("cutover", "decommission")),
}

CASES: tuple[Case, ...] = (
    Case(
        id="jira-cloud-migration",
        input={"prd": "jira-cloud-migration"},
        expect=(
            "output-is-acyclic",
            "proposes-no-unknown-tasks",
            "proposes-no-cycles",
            "orders-the-required-pairs",
        ),
        tags=("dependency", "flagship"),
    ),
    Case(
        id="product-launch",
        input={"prd": "product-launch"},
        expect=("output-is-acyclic", "proposes-no-unknown-tasks", "proposes-no-cycles"),
        tags=("dependency",),
    ),
)


def preflight() -> None:
    if not get_settings().anthropic_api_key:
        raise RuntimeError("LPA_ANTHROPIC_API_KEY is not set. This subject drives a real model.")


def prompt_version() -> str:
    return f"dep-sha256:{hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]}"


def version() -> SubjectVersion:
    from mcp_server import __version__ as code_version

    return SubjectVersion(
        subject=NAME,
        code_version=code_version,
        model=get_settings().anthropic_model or DEFAULT_MODEL,
        prompt_version=prompt_version(),
    )


def _golden_tasks(name: str) -> list[Task]:
    """The hand-authored task list, so this subject is scored in isolation.

    Feeding it a freshly generated breakdown would make a dependency failure
    unattributable — a missing edge could mean the dependency agent missed it or
    the breakdown never proposed the task. Frozen input, one variable.
    """
    path = planning.FIXTURE_DIR / name / "golden" / "expected-plan.json"
    plan = json.loads(path.read_text())
    return [Task(**t) for t in plan["tasks"]]


def _constraints(name: str) -> list[Constraint]:
    path = planning.FIXTURE_DIR / name / "constraints.json"
    if not path.exists():
        return []
    return [Constraint(**c) for c in json.loads(path.read_text())]


def _acyclic(result: DependencyResult) -> CharacteristicResult:
    graph = nx.DiGraph()
    for edge in result.dependencies:
        graph.add_edge(edge.predecessor_id, edge.successor_id)
    cycles = list(nx.simple_cycles(graph))
    return CharacteristicResult(
        name="output-is-acyclic",
        passed=not cycles,
        detail=(
            "no cycles in the returned graph (guards resolve_cycles, not the model)"
            if not cycles
            else f"{len(cycles)} cycle(s) survived repair: {cycles[:2]}"
        ),
    )


def _no_unknown_tasks(result: DependencyResult) -> CharacteristicResult:
    return CharacteristicResult(
        name="proposes-no-unknown-tasks",
        passed=not result.rejections,
        detail=(
            "every proposed edge named a real task"
            if not result.rejections
            else f"{len(result.rejections)} edge(s) rejected: "
            + "; ".join(str(r) for r in result.rejections[:2])
        ),
    )


def _no_cycles_proposed(result: DependencyResult) -> CharacteristicResult:
    return CharacteristicResult(
        name="proposes-no-cycles",
        passed=not result.cycle_breaks,
        detail=(
            "proposed a schedulable graph with no repair needed"
            if not result.cycle_breaks
            else f"{len(result.cycle_breaks)} edge(s) removed to break cycles"
        ),
    )


def _required_order(
    case: Case, result: DependencyResult, tasks: list[Task]
) -> CharacteristicResult:
    """Precedence the PRD states outright, checked through the graph.

    Reachability rather than a direct edge: "migrate before cutover" is honoured
    whether the agent drew one edge or routed it through validation, and
    demanding the direct edge would fail a correct plan for being more detailed.
    """
    names = {t.id: t.name.lower() for t in tasks}
    graph = nx.DiGraph()
    for edge in result.dependencies:
        graph.add_edge(edge.predecessor_id, edge.successor_id)

    missing = []
    for before, after in _REQUIRED_ORDER.get(case.id, ()):
        heads = [i for i, n in names.items() if before in n]
        tails = [i for i, n in names.items() if after in n]
        if not heads or not tails:
            missing.append(f"no task matching {before!r} or {after!r} to order")
            continue
        reachable = any(
            h in graph and t in graph and nx.has_path(graph, h, t) for h in heads for t in tails
        )
        if not reachable:
            missing.append(f"{before} does not precede {after}")
    return CharacteristicResult(
        name="orders-the-required-pairs",
        passed=not missing,
        detail="; ".join(missing) if missing else "required precedence holds",
    )


def run(case: Case, tmp_root: Path, client=None) -> CaseResult:
    name = case.input["prd"]
    prd = planning.prd_text(name)
    tasks = _golden_tasks(name)
    started = time.perf_counter()
    try:
        agent = DependencyAgent(model=get_settings().anthropic_model, client=client or _client())
        result = agent.run(prd, tasks, _constraints(name))
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            usage=Usage(latency_ms=(time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = (time.perf_counter() - started) * 1000

    results = [_acyclic(result), _no_unknown_tasks(result), _no_cycles_proposed(result)]
    if "orders-the-required-pairs" in case.expect:
        results.append(_required_order(case, result, tasks))

    return CaseResult(
        case_id=case.id,
        characteristics=results,
        usage=_usage(agent, latency_ms),
        observations={
            "tasks_in": len(tasks),
            "edges_out": len(result.dependencies),
            "rejected": len(result.rejections),
            "cycle_breaks": len(result.cycle_breaks),
        },
    )


def _usage(agent: DependencyAgent, latency_ms: float) -> Usage:
    used = getattr(agent, "last_usage", None)
    if used is None:
        return Usage(latency_ms=latency_ms)
    return Usage(
        input_tokens=used.input_tokens,
        output_tokens=used.output_tokens,
        cost_usd=cost_usd(used.model, used.input_tokens, used.output_tokens),
        latency_ms=latency_ms,
    )


def _client():
    """Built here rather than left to the agent's default.

    `Agent._default_client()` constructs `anthropic.Anthropic()` bare, which
    reads `ANTHROPIC_API_KEY` — but this repo's key is `LPA_ANTHROPIC_API_KEY`,
    so the default authenticates only by coincidence on a machine that happens
    to have both set. Passing the resolved key is the same thing
    `subjects/status_narrative.py` does, for the same reason.
    """
    import anthropic

    preflight()
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
