"""Status goldens: must-say, must-not-say, and the two edges.

The must-say checks needed the same care as the unsupported-claim ones, for the
mirror reason. A check that reports an omission for a fact the narrative plainly
stated is as corrosive as one that flags a correct claim — both end muted.
"""

from __future__ import annotations

from evals import groundedness
from evals.seedgen import FACT_SETS
from evals.subjects import status_fallback, status_narrative


def _case(index):
    return next(c for c in status_narrative.CASES if c.input["fact_index"] == index)


# --- must-say ---------------------------------------------------------------


def test_required_facts_are_derived_not_hand_listed():
    """A fact set added later must not ship with an empty expectation list and
    score a silent pass — the failure mode of every hand-maintained golden."""
    for facts in FACT_SETS:
        assert status_narrative._must_say(facts), f"{facts.period_label} has no required facts"


def test_an_identifier_fact_may_be_stated_in_prose():
    """Caught by a real run: the check demanded the literal
    `constraint-regulatory-cutoff` in an executive update. No competent
    narrative quotes a constraint id."""
    assert groundedness.mentions("We missed the regulatory cutoff.", "constraint-regulatory-cutoff")
    assert groundedness.mentions("Legal sign-off slipped.", "task-legal-sign-off")
    assert not groundedness.mentions("All quiet.", "constraint-regulatory-cutoff")


def test_a_date_counts_however_it_is_written():
    assert groundedness.mentions("Launch moves to November 13, 2026.", "2026-11-13")
    assert groundedness.mentions("Launch moves to 2026-11-13.", "2026-11-13")
    assert not groundedness.mentions("Launch moves to November 14, 2026.", "2026-11-13")


def test_an_omitted_fact_is_reported_with_what_is_missing():
    found = groundedness.missing("Nothing much happened.", ["2026-11-13", "Legal sign-off"])
    assert len(found) == 2
    assert found[0].kind == "omitted_required_fact"
    assert "2026-11-13" in found[0].detail


# --- the two edges ----------------------------------------------------------


def test_the_no_baseline_case_is_covered():
    """ "Nothing to compare" and "nothing changed" mean opposite things."""
    index = next(i for i, f in enumerate(FACT_SETS) if f.baseline_version is None)
    assert "says-there-is-nothing-to-compare" in _case(index).expect


def test_the_quiet_week_case_is_covered():
    quiet = [
        i
        for i, f in enumerate(FACT_SETS)
        if f.baseline_version is not None
        and not any((f.slipped, f.newly_critical, f.breaches, f.launch_shift_days))
    ]
    assert quiet, "no genuinely quiet week in the fact sets"
    assert "does-not-manufacture-activity" in _case(quiet[0]).expect


def test_the_deterministic_narrative_fails_the_no_baseline_case(tmp_path):
    """A recorded finding, not an accident. `fallback_narrative` writes "No
    material changes since the baseline" when there is no baseline — asserting a
    comparison that never happened. Both shipped callers guard against reaching
    it, so this is latent rather than live; the test pins it so a fix flips it
    deliberately rather than silently."""
    index = next(i for i, f in enumerate(FACT_SETS) if f.baseline_version is None)
    result = status_fallback.run(_case(index), tmp_path)

    failed = [c for c in result.characteristics if not c.passed]
    assert [c.name for c in failed] == ["says-there-is-nothing-to-compare"]


# --- the two subjects are separately versioned ------------------------------


def test_the_two_producers_are_separate_subject_versions():
    """RC1-252 requires them scored as separate subject versions; the run record
    is where that becomes real."""
    llm, fallback = status_narrative.version(), status_fallback.version()

    assert llm.subject != fallback.subject
    assert llm.model and llm.prompt_version, "the LLM run must record what produced it"
    assert fallback.model is None and fallback.prompt_version is None


def test_both_producers_run_the_same_cases():
    """Comparable means the same cases, not merely the same rubric."""
    assert status_fallback.CASES is status_narrative.CASES


def test_the_prompt_version_moves_with_the_prompt(monkeypatch):
    before = status_narrative.prompt_version()
    monkeypatch.setattr(status_narrative, "SYSTEM_PROMPT", "a different prompt")
    assert status_narrative.prompt_version() != before


def test_the_deterministic_twin_needs_no_credentials(tmp_path, monkeypatch):
    """It is the half that can run in CI on every push."""
    monkeypatch.delenv("LPA_ANTHROPIC_API_KEY", raising=False)
    result = status_fallback.run(_case(2), tmp_path)
    assert result.error is None
    assert result.usage.cost_usd == 0
