"""Spec Quality Gate — the deterministic half (RC1-229).

Reviews a PRD *before* anyone plans against it: structural checks, verbatim-quote
verification, and a readiness score. The upstream twin of the plan validation in
`planner_core.validation`, and it deliberately shares that module's machinery
rather than growing a second copy:

- section parsing builds on ``markdown_sections`` (RC1-286),
- quote verification generalizes ``flag_unverifiable_quotes`` and matches through
  ``normalize_for_quote_match`` (RC1-290) — one matcher, one set of hard-won
  false-positive rules (see the RC1-257 note on ``_DECORATION``).

The LLM rubric lives in ``agents.spec_review`` (RC1-289); this subpackage stays
inside the deterministic core, so the root import-linter contract already
forbids it from importing ``agents`` or ``anthropic`` — see ADR-0038.
"""

from __future__ import annotations

from planner_core.spec_gate.models import (
    SPEC_SEVERITY_ORDER,
    FindingCategory,
    SpecFinding,
    SpecReview,
    SpecSeverity,
    SpecVerdict,
    StructuralFinding,
)

__all__ = [
    "SPEC_SEVERITY_ORDER",
    "FindingCategory",
    "SpecFinding",
    "SpecReview",
    "SpecSeverity",
    "SpecVerdict",
    "StructuralFinding",
]
