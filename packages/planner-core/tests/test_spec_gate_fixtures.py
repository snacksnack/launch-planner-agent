"""RC1-287 — the spec-gate corpus and its golden findings stay loadable and honest."""

import json
from collections import Counter
from pathlib import Path

from planner_core.spec_gate import (
    FindingCategory,
    SpecReview,
    normalize_for_quote_match,
    parse_sections,
)

SPEC_GATE = Path(__file__).resolve().parents[3] / "fixtures" / "spec-gate"
GOLDEN = json.loads((SPEC_GATE / "golden-findings.json").read_text())


def _review(name: str) -> SpecReview:
    return SpecReview.model_validate(GOLDEN[name])


def test_golden_loads_through_the_model_for_every_fixture():
    for name in GOLDEN:
        review = _review(name)
        assert review.source_document == f"fixtures/spec-gate/{name}"
        assert (SPEC_GATE / name).is_file(), f"golden references missing fixture {name}"


def test_every_golden_quote_is_locatable_in_its_source():
    """A golden with a stale quote silently weakens every later measurement."""
    for name in GOLDEN:
        haystack = normalize_for_quote_match((SPEC_GATE / name).read_text())
        for finding in _review(name).findings:
            assert normalize_for_quote_match(finding.quote) in haystack, (name, finding.quote)


def test_every_golden_section_ref_is_a_real_heading():
    for name in GOLDEN:
        headings = {s.heading for s in parse_sections((SPEC_GATE / name).read_text())}
        for finding in _review(name).findings:
            assert finding.section in headings, (name, finding.section)


def test_vague_spec_plants_at_least_two_defects_per_category():
    by_category = Counter(f.category for f in _review("vague-spec.md").findings)
    for category in FindingCategory:
        assert by_category[category] >= 2, f"corpus is thin on {category.value}"


def test_good_spec_golden_is_empty():
    """Anything the rubric flags on the good spec is a false positive, by definition."""
    review = _review("good-spec.md")
    assert review.findings == []
    assert review.structural_findings == []


def test_good_and_vague_specs_differ_in_structure_not_just_defect_count():
    vague = {s.heading for s in parse_sections((SPEC_GATE / "vague-spec.md").read_text())}
    good = {s.heading for s in parse_sections((SPEC_GATE / "good-spec.md").read_text())}
    # Different subjects produce mostly different section sets; a near-identical
    # outline would mean the clean sample is the dirty one minus edits.
    assert len(vague & good) <= 1
