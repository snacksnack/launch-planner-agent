"""`Case` — the golden contract: frozen, and never empty of expectations."""

from __future__ import annotations

import pytest
from evals.case import Case
from pydantic import ValidationError


def test_a_case_is_frozen():
    """Ground truth a scorer could edit mid-run is not ground truth."""
    case = Case(id="c1", expect=("something",))
    with pytest.raises(ValidationError):
        case.id = "c2"


def test_a_case_must_expect_something():
    """A case with no expectations always passes, inflating the pass rate while
    measuring nothing — the failure mode this validator exists to prevent."""
    with pytest.raises(ValidationError, match="at least one characteristic"):
        Case(id="c1", expect=())


def test_duplicate_expectations_are_rejected():
    """A repeated name would be scored twice and double-weight one property."""
    with pytest.raises(ValidationError, match="duplicate characteristic"):
        Case(id="c1", expect=("a", "a"))


def test_unknown_fields_are_rejected():
    """`extra="forbid"` house rule: a typo'd field name must not be silently
    accepted and then silently ignored by every scorer."""
    with pytest.raises(ValidationError):
        Case(id="c1", expect=("a",), expceted_output="nope")


def test_input_defaults_to_empty_and_holds_world_state():
    """`input` is not an argument list — for a no-argument subject it carries
    the state the subject runs against."""
    assert Case(id="c1", expect=("a",)).input == {}
    assert Case(id="c2", input={"snapshots": 3}, expect=("a",)).input["snapshots"] == 3
