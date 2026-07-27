"""Tests for deterministic Jira generation (RC1-193)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from planner_core import (
    Confidence,
    Dependency,
    Epic,
    MockJiraTarget,
    Plan,
    Provenance,
    Task,
    TeamMember,
    ThreePointEstimate,
    apply_keys_to_plan,
    build_generation_plan,
    execute_generation,
    schedule_plan,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "jira-cloud-migration"
MONDAY = date(2026, 8, 3)
NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _prov(quote: str = "q") -> Provenance:
    return Provenance(
        reasoning="because", source_quote=quote, source_section="Scope",
        confidence=Confidence.HIGH, agent="a", model="m", timestamp=NOW,
    )


def _task(tid: str, likely: float, *, epic=None, owner=None, jira_key=None) -> Task:
    return Task(
        id=tid, name=tid, epic_id=epic, owner_id=owner, jira_key=jira_key,
        estimate=ThreePointEstimate(optimistic=likely, likely=likely, pessimistic=likely),
        provenance=_prov(),
    )


def _dep(pred: str, succ: str) -> Dependency:
    return Dependency(
        id=f"d-{pred}-{succ}", predecessor_id=pred, successor_id=succ, provenance=_prov()
    )


def _plan(**kw) -> Plan:
    return Plan(id="p", name="p", team=[TeamMember(id="tm-1", name="Ada")], **kw)


# A small plan: 1 epic, 2 tasks (A -> B), owner + epic set.
def _small() -> Plan:
    return _plan(
        epics=[Epic(id="epic-1", name="Data Migration", provenance=_prov())],
        tasks=[_task("A", 5, epic="epic-1", owner="tm-1"), _task("B", 3, epic="epic-1")],
        dependencies=[_dep("A", "B")],
    )


class _SpyTarget:
    """A JiraTarget that records every call — to prove any target gets identical
    operations from a generation plan (the mock==real guarantee, structurally)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._n = 0

    def create_issue(self, **kw):
        self._n += 1
        self.calls.append(("create", kw))
        return f"KEY-{self._n}"

    def update_issue(self, key, **kw):
        self.calls.append(("update", key, kw))

    def create_link(self, **kw):
        self.calls.append(("link", kw))


# --- mapping ---------------------------------------------------------------


def test_generation_maps_epics_stories_links_dates_and_provenance():
    plan = _small()
    gen = build_generation_plan(plan, schedule_plan(plan, start_date=MONDAY), project_key="PMA")

    assert gen.creates == 3  # 1 epic + 2 stories
    assert len(gen.links) == 1
    epic_op = next(op for op in gen.issues if op.issue_type == "Epic")
    assert epic_op.summary == "Data Migration"
    a_op = next(op for op in gen.issues if op.local_id == "A")
    assert a_op.issue_type == "Story"
    assert a_op.parent_local_id == "epic-1"
    assert a_op.due_date is not None  # from the schedule
    assert a_op.owner_name == "Ada"
    assert "data-migration" in a_op.labels
    # Provenance travels into the description.
    assert "Reasoning: because" in a_op.description
    assert "critical" in a_op.description.lower() or a_op.due_date is not None
    (link,) = gen.links
    assert (link.outward_local_id, link.inward_local_id) == ("A", "B")  # A blocks B


def test_flagship_golden_generates_epics_stories_and_all_dependency_links():
    plan = Plan.model_validate_json((FIXTURE / "golden" / "expected-plan.json").read_text())
    gen = build_generation_plan(plan, schedule_plan(plan, start_date=MONDAY), project_key="PMA")
    epics = [op for op in gen.issues if op.issue_type == "Epic"]
    stories = [op for op in gen.issues if op.issue_type == "Story"]
    assert len(epics) == 6
    assert len(stories) == 23
    # 28 task->task deps; milestone links are excluded (milestones aren't issues).
    assert len(gen.links) == 28


# --- execution: mock, spy, ordering ----------------------------------------


def test_execution_creates_issues_then_links_via_the_target():
    plan = _small()
    gen = build_generation_plan(plan, schedule_plan(plan, start_date=MONDAY), project_key="PMA")
    target = MockJiraTarget(project_key="PMA")
    result = execute_generation(gen, target)

    assert len(result.created) == 3
    assert result.linked == 1
    assert len(target.created) == 3 and len(target.links) == 1
    # Epic created before the stories that parent to it.
    assert target.created[0]["issue_type"] == "Epic"
    # The story's parent_key resolved to the epic's new key.
    story = next(c for c in target.created if c["issue_type"] == "Story")
    assert story["parent_key"] == target.created[0]["key"]


def test_any_target_receives_the_same_operations():
    plan = _small()
    gen = build_generation_plan(plan, schedule_plan(plan, start_date=MONDAY), project_key="PMA")
    spy = _SpyTarget()
    execute_generation(gen, spy)
    kinds = [c[0] for c in spy.calls]
    assert kinds == ["create", "create", "create", "link"]  # epic, 2 stories, 1 link


def test_partial_approval_only_runs_selected_ops():
    plan = _small()
    gen = build_generation_plan(plan, schedule_plan(plan, start_date=MONDAY), project_key="PMA")
    target = MockJiraTarget(project_key="PMA")
    result = execute_generation(gen, target, only={"epic-1", "A"})  # skip B

    assert "B" in result.skipped
    assert len(result.created) == 2  # epic + A
    # The A->B link is dropped because B was never created.
    assert result.linked == 0


# --- idempotency -----------------------------------------------------------


def test_rerun_after_writing_keys_produces_zero_duplicates():
    plan = _small()
    sched = schedule_plan(plan, start_date=MONDAY)
    first = execute_generation(
        build_generation_plan(plan, sched, project_key="PMA"), MockJiraTarget("PMA")
    )
    # Write the created keys back, re-generate, re-run.
    mapped = apply_keys_to_plan(plan, first.key_by_local_id)
    assert mapped.tasks[0].jira_key is not None and mapped.epics[0].jira_key is not None

    gen2 = build_generation_plan(mapped, sched, project_key="PMA")
    assert gen2.creates == 0 and gen2.updates == 3
    target2 = MockJiraTarget("PMA")
    result2 = execute_generation(gen2, target2)
    assert result2.created == []  # nothing duplicated
    assert len(result2.updated) == 3


def test_apply_keys_only_maps_known_ids():
    plan = _small()
    mapped = apply_keys_to_plan(plan, {"A": "PMA-9"})
    assert next(t for t in mapped.tasks if t.id == "A").jira_key == "PMA-9"
    assert next(t for t in mapped.tasks if t.id == "B").jira_key is None
