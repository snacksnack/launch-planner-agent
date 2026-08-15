"""The deterministic groundedness layer.

Most of these are regression tests for false positives found by running the
checker over the 36 real seeds in `apps/evals/calibration` and reading the flags.
That corpus is the reason this layer is trustworthy: a checker that cries wolf
gets muted, and a muted checker catches nothing.
"""

from __future__ import annotations

from agent_evals import groundedness
from agent_evals.seeds import SeedStore
from evals.config import SEEDS_PATH

FACTS = {
    "period_label": "Week of 2026-08-17",
    "health": "red",
    "launch_before": "2026-10-12",
    "launch_after": "2026-11-13",
    "launch_shift_days": 24,
    "slipped": [{"id": "task-legal-sign-off", "name": "Legal sign-off", "shift_days": 30}],
}


# --- the acceptance criteria ------------------------------------------------


def test_a_fabricated_ticket_key_is_caught_with_no_judge():
    """RC1-251's headline: the most embarrassing failure, caught for free."""
    report = groundedness.check("Blocked on RC1-999, per the plan.", {"keys": ["RC1-123"]})

    assert not report.grounded
    assert report.violations[0].kind == "invented_ticket_key"
    assert "RC1-999" in report.violations[0].detail


def test_a_real_ticket_key_is_not_flagged():
    assert groundedness.check("Blocked on RC1-123.", {"keys": ["RC1-123"]}).grounded


def test_softening_a_red_finding_is_caught_as_a_contradiction():
    """The other acceptance criterion. True in every particular and still wrong:
    the model does not get to decide severity."""
    report = groundedness.check("The project remains on track for the launch date.", FACTS)

    kinds = [v.kind for v in report.violations]
    assert "contradicted_health" in kinds
    assert (
        "health is 'red'"
        in next(v for v in report.violations if v.kind == "contradicted_health").detail
    )


def test_the_hallucination_rate_is_a_rate_not_a_boolean():
    """One invention in a long narrative and a narrative that is all invention
    are different findings."""
    mostly_fine = groundedness.check(
        "Launch moved to 2026-11-13 after a 24 working day slip. See RC1-999.", FACTS
    )
    assert 0 < mostly_fine.hallucination_rate < 0.5
    assert groundedness.check("Launch moved to 2026-11-13.", FACTS).hallucination_rate == 0.0


def test_the_rate_across_a_run_weights_by_claims_not_by_output():
    """A one-line all-clear should not be averaged against a long narrative as
    though the two carried equal evidence."""
    long_clean = groundedness.check(
        "Launch moved from 2026-10-12 to 2026-11-13, a 24 working day slip.", FACTS
    )
    short_bad = groundedness.check("See RC1-999.", FACTS)
    combined = groundedness.rate_across([long_clean, short_bad])

    assert 0 < combined < short_bad.hallucination_rate


# --- false positives found on real output -----------------------------------


def test_a_date_embedded_in_a_fact_string_counts_as_support():
    """The facts carry `period_label: "Week of 2026-08-17"`. The first version
    matched whole field values only, so a narrative correctly writing
    "August 17, 2026" was flagged as inventing it."""
    assert groundedness.check("For the week of August 17, 2026, the plan slipped.", FACTS).grounded


def test_a_health_word_about_part_of_the_work_is_not_a_health_claim():
    """Real output: "core engineering and product work remains on track" — true,
    and says nothing about overall health."""
    report = groundedness.check(
        "Core engineering and product work remains on track; the delay is isolated.", FACTS
    )
    assert not [v for v in report.violations if v.kind == "contradicted_health"]


def test_a_negated_health_word_is_not_an_assertion():
    """Real output: "health remains green, with no red flags" — a green sentence
    containing the word "red"."""
    green = {**FACTS, "health": "green"}
    report = groundedness.check(
        "Overall project health remains green, with no red flags this period.", green
    )
    assert not [v for v in report.violations if v.kind == "contradicted_health"]


def test_a_health_word_in_a_different_sense_is_not_a_claim():
    """Real output: "reflects a healthy buffer for final validation"."""
    yellow = {**FACTS, "health": "yellow"}
    report = groundedness.check(
        "The revised date reflects a healthy buffer for final validation.", yellow
    )
    assert not [v for v in report.violations if v.kind == "contradicted_health"]


def test_a_day_count_that_is_in_the_facts_is_not_flagged():
    """`launch_shift_days: 24` supports "24 working days"; the sign lives in the
    prose, so an absolute-value match is correct."""
    pulled_in = {"launch_shift_days": -3, "health": "green"}
    assert groundedness.check("The launch pulled in 3 working days.", pulled_in).grounded


def test_an_invented_day_count_is_flagged():
    report = groundedness.check("The launch slipped 99 working days.", FACTS)
    assert [v for v in report.violations if v.kind == "invented_day_count"]


# --- must-not-say -----------------------------------------------------------


def test_declared_must_not_say_is_first_class():
    """Beside must-say, not after it: an output can state only true things and
    still violate its brief."""
    found = groundedness.must_not_say(
        "The team is confident this will be resolved.", ["the team is confident"]
    )
    assert found and found[0].kind == "said_forbidden_thing"
    assert not groundedness.must_not_say(
        "Legal sign-off slipped 30 days.", ["the team is confident"]
    )


# --- the corpus check that keeps this honest --------------------------------


def test_no_false_positives_on_the_real_clean_seeds():
    """`fallback` outputs are a template over the facts and cannot be ungrounded.
    Any flag here is a bug in the checker, not a finding about the output — and
    this is the assertion that caught the date bug and all three health ones."""
    seeds = [s for s in SeedStore(SEEDS_PATH).all() if s.variant == "fallback"]
    assert seeds, "the committed seed set is missing"

    for seed in seeds:
        report = groundedness.check(seed.rendered_output(), seed.facts)
        assert report.grounded, f"{seed.id}: {[v.detail for v in report.violations]}"


def test_the_shipped_agent_is_clean_on_the_real_seeds():
    """Not a guarantee about the agent — a record of where it stands. If this
    starts failing, either the agent regressed or the checker got stricter, and
    the violation detail says which."""
    seeds = [s for s in SeedStore(SEEDS_PATH).all() if s.variant == "agent"]
    flagged = [
        (s.id, [v.detail for v in groundedness.check(s.rendered_output(), s.facts).violations])
        for s in seeds
        if not groundedness.check(s.rendered_output(), s.facts).grounded
    ]
    assert not flagged, flagged


def test_the_planted_degradation_is_detected_on_the_real_seeds():
    """The layer has to actually catch something, or precision is free."""
    seeds = [s for s in SeedStore(SEEDS_PATH).all() if s.variant == "degraded"]
    caught = [s for s in seeds if not groundedness.check(s.rendered_output(), s.facts).grounded]
    assert caught, "the deterministic layer caught nothing in the degraded variant"
