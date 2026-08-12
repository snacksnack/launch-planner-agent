"""Plan-reference resolution.

The reference problem is the whole point of RC1-237: a model will not reliably
pick between a version integer, a 64-character hash, and a file path, so the
friendly forms have to work and the wrong forms have to fail in a way that tells
the model what to do next.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.config import get_settings
from app.store import SQLiteEventStore
from mcp_server.errors import AmbiguousPlanRef, PlanNotFound
from mcp_server.resolve import resolve_plan_ref, snapshot_history
from planner_core import Plan, Snapshot, SnapshotKind, content_hash

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "fixtures/jira-cloud-migration/golden/expected-plan.json"
)


def _plan(name: str | None = None) -> Plan:
    plan = Plan.model_validate_json(GOLDEN.read_text())
    if name is not None:
        plan = plan.model_copy(update={"name": name})
    return plan


def _commit(plan: Plan, kind: SnapshotKind, when: datetime) -> Snapshot:
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        return store.append(
            Snapshot(
                kind=kind,
                plan=plan,
                content_hash=content_hash(plan),
                created_at=when,
                approved_by="test",
                message=f"{kind.value} of {plan.name}",
            )
        )
    finally:
        store.close()


def _seed(n: int = 3) -> list[Snapshot]:
    """n snapshots with distinct content, so hashes differ."""
    base = datetime.now(UTC)
    return [
        _commit(_plan(f"Plan {i}"), SnapshotKind.COMMIT, base + timedelta(minutes=i))
        for i in range(n)
    ]


# --- the default plan -------------------------------------------------------


def test_no_ref_resolves_the_configured_default_file():
    resolved = resolve_plan_ref(None)
    assert resolved.source == "file"
    assert resolved.version is None
    assert resolved.path is not None
    assert resolved.plan.tasks


def test_blank_and_whitespace_refs_are_treated_as_omitted():
    assert resolve_plan_ref("").source == "file"
    assert resolve_plan_ref("   ").source == "file"


def test_the_default_plans_canonical_ref_round_trips():
    """A file plan is not in the store, so echoing its content hash back would
    resolve to nothing. `canonical_ref` must be something we accept."""
    first = resolve_plan_ref(None)
    assert first.canonical_ref == "default"
    again = resolve_plan_ref(first.canonical_ref)
    assert again.content_hash == first.content_hash


def test_a_missing_default_file_says_what_to_do_instead(monkeypatch):
    monkeypatch.setenv("LPA_PLAN_PATH", "does/not/exist.json")
    get_settings.cache_clear()
    with pytest.raises(PlanNotFound) as caught:
        resolve_plan_ref(None)
    assert "plan.list" in str(caught.value)


# --- snapshots --------------------------------------------------------------


def test_a_version_number_resolves_that_snapshot():
    _seed(3)
    resolved = resolve_plan_ref("2")
    assert resolved.source == "snapshot"
    assert resolved.version == 2
    assert resolved.plan.name == "Plan 1"


def test_latest_resolves_the_newest_record():
    _seed(3)
    assert resolve_plan_ref("latest").version == 3


def test_baseline_resolves_the_newest_baseline_not_the_newest_commit():
    _seed(2)
    _commit(_plan("Baselined"), SnapshotKind.BASELINE, datetime.now(UTC))
    _commit(_plan("Later commit"), SnapshotKind.COMMIT, datetime.now(UTC))

    assert resolve_plan_ref("latest").plan.name == "Later commit"
    assert resolve_plan_ref("baseline").plan.name == "Baselined"


def test_refs_are_case_insensitive():
    _seed(1)
    assert resolve_plan_ref("LATEST").version == 1


def test_canonical_ref_for_a_snapshot_is_its_version():
    _seed(2)
    resolved = resolve_plan_ref("latest")
    assert resolved.canonical_ref == "2"
    assert resolve_plan_ref(resolved.canonical_ref).content_hash == resolved.content_hash


# --- hash prefixes ----------------------------------------------------------


def test_a_hash_prefix_resolves_when_it_is_unique():
    snapshots = _seed(3)
    prefix = snapshots[1].content_hash[:10]
    assert resolve_plan_ref(prefix).version == 2


def test_the_full_hash_also_works():
    snapshots = _seed(2)
    assert resolve_plan_ref(snapshots[0].content_hash).version == 1


def test_an_ambiguous_prefix_lists_the_candidates_instead_of_guessing():
    """Silently picking one would make the model report a date computed from a
    plan the user did not ask about, with nothing downstream to reveal it."""
    plan = _plan("Same content")
    _commit(plan, SnapshotKind.PROPOSAL, datetime.now(UTC))
    _commit(plan, SnapshotKind.COMMIT, datetime.now(UTC))

    shared_prefix = content_hash(plan)[:8]
    with pytest.raises(AmbiguousPlanRef) as caught:
        resolve_plan_ref(shared_prefix)
    rendered = str(caught.value)
    assert "matches 2 snapshots" in rendered
    assert "v1" in rendered and "v2" in rendered
    assert caught.value.candidates


def test_a_too_short_prefix_is_rejected_with_the_alternatives():
    _seed(1)
    with pytest.raises(PlanNotFound) as caught:
        resolve_plan_ref("ab")
    rendered = str(caught.value)
    assert "at least 4" in rendered
    assert "latest" in rendered


def test_an_unmatched_prefix_points_at_plan_list():
    _seed(1)
    with pytest.raises(PlanNotFound) as caught:
        resolve_plan_ref("ffffffff")
    assert "plan.list" in str(caught.value)


# --- empty store ------------------------------------------------------------


def test_latest_on_an_empty_store_explains_rather_than_returning_nothing():
    with pytest.raises(PlanNotFound) as caught:
        resolve_plan_ref("latest")
    rendered = str(caught.value)
    assert "nothing has been committed" in rendered
    assert "Omit the reference" in rendered


def test_baseline_on_an_empty_store_says_baseline_specifically():
    with pytest.raises(PlanNotFound) as caught:
        resolve_plan_ref("baseline")
    assert "No baseline exists" in str(caught.value)


def test_a_missing_version_lists_what_is_available():
    _seed(2)
    with pytest.raises(PlanNotFound) as caught:
        resolve_plan_ref("99")
    assert "Available versions: 1, 2" in str(caught.value)


# --- paths are not a caller-supplied reference ------------------------------


def test_a_file_path_is_not_accepted_as_a_reference():
    """The HTTP API takes a `plan=` path; exposing that to a model would let a
    conversation read arbitrary JSON off the host for no benefit."""
    with pytest.raises(PlanNotFound):
        resolve_plan_ref(str(GOLDEN))
    with pytest.raises(PlanNotFound):
        resolve_plan_ref("../../etc/passwd")


# --- history ----------------------------------------------------------------


def test_history_is_oldest_first_and_read_only():
    _seed(3)
    versions = [s.version for s in snapshot_history()]
    assert versions == [1, 2, 3]
    assert [s.version for s in snapshot_history()] == versions
