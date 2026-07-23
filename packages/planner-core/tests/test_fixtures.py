"""Load every fixture corpus through the P1.2 model and check it is coherent.

This is the RC1-184 acceptance guard: the flagship jira-cloud-migration fixture
and the smaller product-launch fixture must load through `planner_core`, survive
a JSON round-trip, and be internally consistent — every id reference resolves,
the dependency graph is acyclic, and every provenance `source_quote` appears
verbatim in the fixture's own PRD (whitespace-normalized). The test discovers
fixture directories automatically, so a future fixture is covered the moment it
lands with the expected file layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest
from planner_core import (
    Constraint,
    Plan,
    TeamMember,
)


def _find_fixtures_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "fixtures"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("could not locate the repo-level fixtures/ directory")


FIXTURES_DIR = _find_fixtures_dir()

# A fixture directory is any subdir with a golden expected-plan.json.
FIXTURE_DIRS = sorted(
    p.parent.parent for p in FIXTURES_DIR.glob("*/golden/expected-plan.json")
)
FIXTURE_IDS = [p.name for p in FIXTURE_DIRS]


def _normalize(text: str) -> str:
    """Collapse all whitespace so quotes match regardless of PRD line wrapping."""
    return " ".join(text.split())


def _load_plan(fixture: Path) -> Plan:
    return Plan.model_validate_json((fixture / "golden" / "expected-plan.json").read_text())


def test_fixtures_were_discovered():
    # Guard against the glob silently matching nothing and every test being skipped.
    assert FIXTURE_DIRS, "no fixture directories found"
    assert "jira-cloud-migration" in FIXTURE_IDS
    assert "product-launch" in FIXTURE_IDS


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_inputs_load_through_the_model(fixture: Path):
    team = [TeamMember.model_validate(x) for x in json.loads((fixture / "team.json").read_text())]
    constraints = [
        Constraint.model_validate(x)
        for x in json.loads((fixture / "constraints.json").read_text())
    ]
    assert team, "team.json should not be empty"
    assert constraints, "constraints.json should not be empty"


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_golden_plan_round_trips(fixture: Path):
    plan = _load_plan(fixture)
    assert Plan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_golden_plan_embeds_the_input_files(fixture: Path):
    """The self-contained golden Plan must agree with the input sidecar files."""
    plan = _load_plan(fixture)
    team = [TeamMember.model_validate(x) for x in json.loads((fixture / "team.json").read_text())]
    constraints = [
        Constraint.model_validate(x)
        for x in json.loads((fixture / "constraints.json").read_text())
    ]
    assert plan.team == team
    assert plan.constraints == constraints


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_referential_integrity(fixture: Path):
    plan = _load_plan(fixture)

    epic_ids = {e.id for e in plan.epics}
    task_ids = {t.id for t in plan.tasks}
    member_ids = {m.id for m in plan.team}
    milestone_ids = {m.id for m in plan.milestones}
    schedulable_ids = task_ids | milestone_ids

    # No duplicate ids within any collection.
    for label, items in [
        ("epics", plan.epics),
        ("tasks", plan.tasks),
        ("dependencies", plan.dependencies),
        ("milestones", plan.milestones),
        ("constraints", plan.constraints),
        ("team", plan.team),
    ]:
        ids = [x.id for x in items]
        assert len(ids) == len(set(ids)), f"duplicate id in {label}"

    for task in plan.tasks:
        if task.epic_id is not None:
            assert task.epic_id in epic_ids, f"{task.id} -> unknown epic {task.epic_id}"
        if task.owner_id is not None:
            assert task.owner_id in member_ids, f"{task.id} -> unknown owner {task.owner_id}"

    for dep in plan.dependencies:
        assert dep.predecessor_id in task_ids, f"{dep.id} -> unknown predecessor"
        assert dep.successor_id in task_ids, f"{dep.id} -> unknown successor"

    for con in plan.constraints:
        for ref in con.applies_to:
            assert ref in schedulable_ids, f"{con.id} applies_to unknown {ref}"


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_dependency_graph_is_acyclic(fixture: Path):
    plan = _load_plan(fixture)
    graph = nx.DiGraph()
    graph.add_nodes_from(t.id for t in plan.tasks)
    graph.add_edges_from((d.predecessor_id, d.successor_id) for d in plan.dependencies)
    assert nx.is_directed_acyclic_graph(graph), "golden dependency graph has a cycle"


@pytest.mark.parametrize("fixture", FIXTURE_DIRS, ids=FIXTURE_IDS)
def test_provenance_quotes_are_verbatim_from_the_prd(fixture: Path):
    """Every source_quote must be traceable, verbatim, to the fixture's PRD."""
    plan = _load_plan(fixture)
    prd = _normalize((fixture / "prd.md").read_text())

    provenanced = [
        *plan.epics,
        *plan.tasks,
        *plan.dependencies,
        *plan.milestones,
        *plan.constraints,
    ]
    assert provenanced, "expected the golden plan to contain provenanced entities"

    misses = [
        (entity.id, entity.provenance.source_quote)
        for entity in provenanced
        if _normalize(entity.provenance.source_quote) not in prd
    ]
    assert not misses, f"source_quote(s) not found verbatim in prd.md: {misses}"
