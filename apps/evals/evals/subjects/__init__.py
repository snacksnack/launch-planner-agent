"""Subjects under test.

A subject is a module exposing three names: `NAME`, `version()`, and `run(case)`,
plus a `CASES` tuple. That is the whole contract, and it is duck-typed on
purpose — no `Subject` protocol, no ABC, no plugin discovery. There is exactly
one subject today; an interface generalised from one implementation is a guess,
and RC1-252 is where the second consumer makes the real seams visible. See
ADR-0030.

`SUBJECTS` is a dict rather than an entry-point registry for the same reason: a
CLI needs to turn `health` into a module, and a dict does that in one line
without anything to configure or discover.
"""

from __future__ import annotations

from evals.subjects import (
    dependency,
    groundedness,
    health,
    raid,
    spec_review,
    spec_structural,
    status_fallback,
    status_narrative,
    tool_selection,
    work_breakdown,
)

SUBJECTS = {
    groundedness.NAME: groundedness,
    status_narrative.NAME: status_narrative,
    status_fallback.NAME: status_fallback,
    health.NAME: health,
    tool_selection.NAME: tool_selection,
    work_breakdown.NAME: work_breakdown,
    dependency.NAME: dependency,
    raid.NAME: raid,
    spec_structural.NAME: spec_structural,
    spec_review.NAME: spec_review,
}

#: Subjects that reach a real model. They cost tokens, need
#: `LPA_ANTHROPIC_API_KEY`, and are therefore not part of `uv run pytest` —
#: which stays credential-free by design. See ADR-0031.
BILLED = frozenset(
    {
        tool_selection.NAME,
        status_narrative.NAME,
        work_breakdown.NAME,
        dependency.NAME,
        raid.NAME,
        spec_review.NAME,
    }
)

__all__ = [
    "BILLED",
    "SUBJECTS",
    "dependency",
    "groundedness",
    "health",
    "raid",
    "spec_review",
    "spec_structural",
    "status_fallback",
    "status_narrative",
    "tool_selection",
    "work_breakdown",
]
