"""RC1-288 — structural checks: exact on the corpus, silent on good input."""

import json
from pathlib import Path

from planner_core.spec_gate import (
    SpecReview,
    parse_sections,
    run_structural_checks,
)
from planner_core.spec_gate.structural import (
    check_countable_criteria,
    check_named_owner,
    check_requirement_ids,
    check_unresolved_markers,
)

SPEC_GATE = Path(__file__).resolve().parents[3] / "fixtures" / "spec-gate"


def _run(name: str):
    return run_structural_checks(parse_sections((SPEC_GATE / name).read_text()))


def test_good_spec_produces_zero_structural_findings():
    """If this fails, either a check is wrong or the fixture is not actually good."""
    assert _run("good-spec.md") == []


def test_vague_spec_matches_the_structural_golden_exactly():
    golden = json.loads((SPEC_GATE / "golden-findings.json").read_text())
    expected = SpecReview.model_validate(golden["vague-spec.md"]).structural_findings
    assert _run("vague-spec.md") == expected


def test_output_order_is_deterministic():
    text = (SPEC_GATE / "vague-spec.md").read_text()
    first = run_structural_checks(parse_sections(text))
    second = run_structural_checks(parse_sections(text))
    assert first == second


def test_checks_run_without_a_credential(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "LPA_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert _run("vague-spec.md")  # runs and finds the plants, no key involved


def test_requirement_id_check_needs_a_requirements_section():
    # No requirements section at all: missing-section territory, not this check's.
    assert check_requirement_ids(parse_sections("# Overview\n\nprose\n")) == []


def test_owner_check_skips_documents_without_an_ownership_section():
    # Unknown shapes yield no finding rather than a spurious one.
    assert check_named_owner(parse_sections("# Goal\n\nship it\n")) == []


def test_uncountable_criteria_fires_on_prose_and_not_on_lists():
    prose = "# Acceptance criteria\n\nIt should work well and users should be happy.\n"
    listed = "# Acceptance criteria\n\n- exporting 400k events delivers a link\n"
    assert [f.code for f in check_countable_criteria(parse_sections(prose))] == [
        "uncountable-criteria"
    ]
    assert check_countable_criteria(parse_sections(listed)) == []


def test_todo_in_a_code_fence_is_not_unfinished_scope():
    doc = "# Requirements\n\nREQ-1 — parse input.\n\n```python\n# TODO refactor\n```\n"
    assert check_unresolved_markers(parse_sections(doc)) == []


def test_todo_in_prose_scope_is_flagged_with_the_line_as_quote():
    doc = "# Scope\n\nBilling migration. TODO: decide on proration.\n"
    findings = check_unresolved_markers(parse_sections(doc))
    assert [f.code for f in findings] == ["unresolved-marker"]
    assert findings[0].quote == "Billing migration. TODO: decide on proration."
    assert findings[0].section == "Scope"


def test_non_goals_heading_does_not_satisfy_the_goal_requirement():
    doc = "# Non-goals\n\n- nothing\n\n# Requirements\n\nREQ-1 — a thing.\n"
    codes = [(f.code, f.message) for f in run_structural_checks(parse_sections(doc))]
    assert ("missing-section", "no section matching 'goal' found") in codes
