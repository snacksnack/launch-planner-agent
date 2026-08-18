"""RC1-286 — section parsing tiles the document; delegation preserves behavior."""

import re
from pathlib import Path

from planner_core.spec_gate import MarkdownFile, parse_sections
from planner_core.validation import markdown_sections

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
PRDS = sorted(FIXTURES.glob("*/prd.md"))

#: The regex `markdown_sections` used before it delegated to the parser.
_OLD_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def test_fixture_prds_exist():
    assert PRDS, "expected at least one fixtures/*/prd.md"


def test_sections_tile_every_fixture_prd_exactly():
    for prd in PRDS:
        source = prd.read_text()
        sections = parse_sections(source)
        assert "".join(s.text for s in sections) == source, prd
        for s in sections:
            assert source[s.start : s.end] == s.text, (prd, s.heading)
            assert source[s.body_start : s.end] == s.body, (prd, s.heading)


def test_markdown_sections_matches_the_old_regex_on_every_fixture():
    """The delegation (RC1-286) must not change the coverage report's view."""
    for prd in PRDS:
        source = prd.read_text()
        old = [m.group(1).strip() for m in _OLD_HEADING.finditer(source)]
        assert markdown_sections(source) == old, prd


def test_heading_inside_code_fence_is_not_a_section():
    doc = "# Real\n\nbody\n\n```bash\n# not a heading\necho hi\n```\n\n## Also real\n"
    sections = parse_sections(doc)
    assert [s.heading for s in sections] == ["Real", "Also real"]
    # The fence content stays inside the first section's body...
    assert "# not a heading" in sections[0].body
    # ...but is stripped from what the rubric may scan.
    assert "not a heading" not in sections[0].prose_body
    assert "body" in sections[0].prose_body


def test_headingless_document_yields_one_section():
    sections = parse_sections("just a flat paragraph, no headings at all\n")
    assert len(sections) == 1
    assert sections[0].heading is None
    assert sections[0].level == 0
    assert sections[0].text == "just a flat paragraph, no headings at all\n"


def test_empty_document_yields_one_empty_section():
    sections = parse_sections("")
    assert len(sections) == 1
    assert sections[0].text == ""


def test_preamble_before_first_heading_is_its_own_section():
    doc = "intro line before any heading\n\n# First\n\nbody\n"
    sections = parse_sections(doc)
    assert sections[0].heading is None
    assert sections[0].text == "intro line before any heading\n\n"
    assert sections[1].heading == "First"
    assert "".join(s.text for s in sections) == doc


def test_heading_levels_and_bodies():
    doc = "# One\nalpha\n## Two\nbeta\n"
    one, two = parse_sections(doc)
    assert (one.heading, one.level, one.body) == ("One", 1, "alpha\n")
    assert (two.heading, two.level, two.body) == ("Two", 2, "beta\n")


def test_unclosed_fence_swallows_the_rest_of_the_body():
    doc = "# H\ntext\n```\n# swallowed\n"
    sections = parse_sections(doc)
    assert [s.heading for s in sections] == ["H"]
    assert sections[0].prose_body == "text\n"


def test_hash_without_space_is_not_a_heading():
    # The old regex required whitespace after the hashes; so does the parser.
    sections = parse_sections("#hashtag not a heading\n")
    assert [s.heading for s in sections] == [None]


def test_markdown_file_source_loads_and_keeps_origin(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# Hello\n")
    loaded = MarkdownFile(spec).load()
    assert loaded.text == "# Hello\n"
    assert loaded.origin == str(spec)  # a real path — RC1-293 depends on this
