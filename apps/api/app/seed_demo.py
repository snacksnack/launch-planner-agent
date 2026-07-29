"""Seed the public demo store so a first-time visitor sees a full history.

The read-only demo has no way to create snapshots (agents + commits are CLI-only),
so the Baseline, Status, and audit-trail views would be empty on a fresh volume.
This seeds — once, only if the store is empty — a realistic history from the
flagship golden: an agent **proposal**, the human **commit**, and an **optimistic
baseline** the current plan then reads as drift against. Deterministic and
credential-free; runs at app startup in `public_demo` mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from planner_core import (
    Plan,
    ThreePointEstimate,
    build_decision_record,
    commit_baseline,
    commit_plan,
    record_proposal,
)

from app.config import get_settings
from app.store import SQLiteEventStore

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _golden() -> Plan:
    path = _REPO_ROOT / get_settings().plan_path
    return Plan.model_validate_json(path.read_text())


def _optimistic(plan: Plan) -> Plan:
    """A more-optimistic earlier baseline: the bulk + pilot waves were planned
    shorter than they turned out, so the committed golden reads as real drift."""
    base = plan.model_copy(deep=True)
    for task in base.tasks:
        if task.id in ("task-bulk-migration", "task-pilot-migration"):
            e = task.estimate
            task.estimate = ThreePointEstimate(
                optimistic=max(1, e.optimistic - 8),
                likely=max(1, e.likely - 8),
                pessimistic=max(1, e.pessimistic - 8),
            )
    return base


def seed_if_empty(sqlite_path: str) -> bool:
    """Seed proposal → commit → baseline if the store has no snapshots. Returns
    True if it seeded, False if the store already had history."""
    store = SQLiteEventStore(sqlite_path)
    try:
        if store.history():
            return False
        golden = _golden()
        prd_path = _REPO_ROOT / (golden.source_document or "")
        prd = prd_path.read_text() if prd_path.is_file() else ""
        now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

        record = build_decision_record(golden, prd)
        record_proposal(store, golden, now=now, message="agent proposal", decision_record=record)
        commit_baseline(
            store, _optimistic(golden), approved_by="Priya Nair (TPM)",
            note="initial optimistic plan", now=now,
        )
        commit_plan(
            store, golden, approved_by="Priya Nair (TPM)",
            now=now, message="reviewed & approved", decision_record=record,
        )
        return True
    finally:
        store.close()


def main() -> None:
    seeded = seed_if_empty(get_settings().sqlite_path)
    print("seeded demo history" if seeded else "store already has history — nothing to seed")


if __name__ == "__main__":
    main()
