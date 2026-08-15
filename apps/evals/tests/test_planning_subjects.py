"""Free checks over the planning subjects (RC1-257).

The subjects themselves are billed and stay out of `pytest` (ADR-0031). What is
tested here is the scoring — which is where the mistakes were. Both of these
pin a false positive that a real run found and a passing test had not.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from evals import planning
from evals.subjects import raid as raid_subject
from planner_core import Confidence, Epic, Provenance, Task, TeamMember, ThreePointEstimate
from planner_core.raid import PrdEvidence, RaidItem, RaidProvenance, RaidType


def _prov(quote: str) -> Provenance:
    return Provenance(
        reasoning="r",
        source_quote=quote,
        source_section=None,
        confidence=Confidence.HIGH,
        agent="work-breakdown",
        model="m",
        timestamp=datetime(2026, 8, 15, tzinfo=UTC),
    )


def _task(id_: str, quote: str, *, epic_id=None, owner_id=None) -> Task:
    return Task(
        id=id_,
        name=id_,
        epic_id=epic_id,
        owner_id=owner_id,
        estimate=ThreePointEstimate(optimistic=1, likely=2, pessimistic=3),
        provenance=_prov(quote),
    )


PRD = "## Scope\n\nWe will **migrate the data** and then cut over.\n"


def test_provenance_tracing_ignores_markdown_but_not_invention():
    """Both directions, because a checker that cannot fail is not a checker."""
    faithful = planning.traces_to_the_prd([], [_task("t-1", "migrate the data")], PRD)
    assert faithful.passed, "the PRD's ** markers are not part of the quote"

    invented = planning.traces_to_the_prd([], [_task("t-2", "rewrite the frontend")], PRD)
    assert not invented.passed
    assert "t-2" in invented.detail


def test_orphan_and_roster_checks_fail_on_bad_references():
    epics = [Epic(id="e-1", name="Migration", provenance=_prov("migrate the data"))]
    roster = [TeamMember(id="tm-1", name="Ada")]

    dangling = planning.no_orphan_tasks(epics, [_task("t-1", "cut over", epic_id="e-nope")])
    assert not dangling.passed and "e-nope" not in dangling.detail.split()[0]

    invented_owner = planning.owners_are_on_the_roster(
        [_task("t-1", "cut over", owner_id="tm-ghost")], roster
    )
    assert not invented_owner.passed and "tm-ghost" in invented_owner.detail

    # A null owner is the sanctioned way to say "unclear" and must not fail.
    assert planning.owners_are_on_the_roster([_task("t-2", "cut over")], roster).passed


def _raid(type_: RaidType, id_: str, probability=None, impact=None) -> RaidItem:
    return RaidItem(
        id=id_,
        type=type_,
        title=id_,
        description="d",
        probability=probability,
        impact=impact,
        provenance=RaidProvenance(
            reasoning="r",
            confidence=Confidence.HIGH,
            evidence=PrdEvidence(source_quote="q"),
            agent="raid",
            model="m",
            timestamp=datetime(2026, 8, 15, tzinfo=UTC),
        ),
    )


def test_low_risk_proportionality_counts_risks_not_the_whole_log():
    """RC1-257: the first version of this capped total RAID entries at four and
    failed a correct answer — two assumptions the PRD stated outright, two risks
    at severity 4/25, and one decision.

    Assumptions and decisions are the A and D of RAID. Counting them as noise
    punishes the agent for doing the job.
    """
    proportionate = [
        _raid(RaidType.ASSUMPTION, "a-1"),
        _raid(RaidType.ASSUMPTION, "a-2"),
        _raid(RaidType.RISK, "r-1", 2, 2),
        _raid(RaidType.RISK, "r-2", 1, 4),
        _raid(RaidType.DECISION, "d-1"),
    ]
    assert raid_subject._proportionate(proportionate).passed, (
        "five entries with two low-severity risks is a proportionate log"
    )

    alarmist = proportionate + [_raid(RaidType.RISK, "r-3", 5, 5)]
    assert not raid_subject._proportionate(alarmist).passed, (
        "a severity-25 risk on a rehearsed version bump is the real failure"
    )


def test_unscored_risks_are_caught_but_assumptions_are_not_required_to_score():
    unscored = [_raid(RaidType.RISK, "r-1")]
    assert not raid_subject._scored(unscored).passed

    # Assumptions have no scoring fields in the schema; demanding them would
    # fail correct output.
    assert raid_subject._scored([_raid(RaidType.ASSUMPTION, "a-1")]).passed


@pytest.mark.parametrize("name", ["thin", "low-risk"])
def test_the_eval_only_prds_load(name):
    assert planning.prd_text(name).strip(), f"{name}.md must exist and be non-empty"
    assert planning.team(name), "eval PRDs borrow the migration roster"
