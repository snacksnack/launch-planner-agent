"""Typed vocabulary of the Spec Quality Gate (RC1-285).

Two finding shapes, deliberately distinct:

- `SpecFinding` — LLM judgment. Inherits `ProvenancedModel`, so the verbatim
  quote, section, explanation, and confidence live in the one `Provenance`
  block every agent-produced entity in this repo already carries. The finding
  exposes them as read-only properties rather than duplicating them as fields —
  one source of truth, nothing to drift.
- `StructuralFinding` — deterministic checks (RC1-288). Provenance-free by
  design: a pure function has no reasoning, model, or confidence to record, and
  stamping a fake `Provenance` on it would dilute the audit trail. Its `quote`
  is optional because absence findings ("no rollback section") have no text to
  point at.

Severity is the PR agent's ladder (blocker / warning / nit), named
`SpecSeverity` because `planner_core` already exports the plan-validation
`Severity` (error / warning) at the package root.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from planner_core.provenance import ProvenancedModel


class SpecSeverity(StrEnum):
    """Same vocabulary as the PR Review Agent's SEVERITY_ORDER, worst first."""

    BLOCKER = "blocker"
    WARNING = "warning"
    NIT = "nit"


#: Sort key: worst first, matching the PR agent's convention.
SPEC_SEVERITY_ORDER = {SpecSeverity.BLOCKER: 0, SpecSeverity.WARNING: 1, SpecSeverity.NIT: 2}


class FindingCategory(StrEnum):
    """The six rubric categories from RC1-229. Closed on purpose — evals score
    recall per category, which free-text categories would make unmeasurable."""

    AMBIGUOUS_QUANTIFIER = "ambiguous_quantifier"
    UNTESTABLE_CRITERION = "untestable_criterion"
    MISSING_NFR = "missing_nfr"
    UNSTATED_ASSUMPTION = "unstated_assumption"
    CONFLICTING_REQUIREMENT = "conflicting_requirement"
    UNOWNED_SCOPE = "unowned_scope"


class SpecVerdict(StrEnum):
    """Set by pure code (RC1-290), gating on category — never by the model.

    Advisory unless a configured block-on category fires; the default
    configuration is empty, so the default verdict is always ADVISORY.
    """

    ADVISORY = "advisory"
    BLOCKED = "blocked"


class SpecFinding(ProvenancedModel):
    """One rubric finding. The quote lives in `provenance.source_quote`.

    `Provenance.source_quote` already enforces non-empty; the extra validator
    here rejects whitespace-only, because a finding that cannot point at real
    text does not exist (RC1-229's central rule, enforced by type).
    """

    category: FindingCategory = Field(..., description="Which rubric category fired.")
    severity: SpecSeverity = Field(..., description="blocker / warning / nit.")
    suggested_rewrite: str | None = Field(
        None,
        description="A proposed replacement for the quoted text. A proposal, never auto-applied.",
    )

    @model_validator(mode="after")
    def _quote_is_real_text(self) -> SpecFinding:
        if not self.provenance.source_quote.strip():
            raise ValueError("a SpecFinding must quote real text; whitespace is not a quote")
        return self

    @property
    def quote(self) -> str:
        """The offending text, verbatim — single-sourced from provenance."""
        return self.provenance.source_quote

    @property
    def section(self) -> str | None:
        return self.provenance.source_section

    @property
    def explanation(self) -> str:
        return self.provenance.reasoning

    @property
    def severity_rank(self) -> int:
        return SPEC_SEVERITY_ORDER[self.severity]


class StructuralFinding(BaseModel):
    """One deterministic-check finding. No provenance, by design (see module doc)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        ...,
        min_length=1,
        description="Stable check identifier (e.g. 'missing-section'), ValidationIssue-style.",
    )
    severity: SpecSeverity = Field(..., description="blocker / warning / nit.")
    message: str = Field(..., min_length=1, description="What the check found, for a human.")
    quote: str | None = Field(
        None,
        description="Verbatim offending text, when the check points at text. Absence findings "
        "(a section that does not exist) carry no quote.",
    )
    section: str | None = Field(
        None, description="Heading of the section the finding is about, when known."
    )


class SpecReview(BaseModel):
    """The gate's full output. A review with zero findings is a valid clean bill."""

    model_config = ConfigDict(extra="forbid")

    source_document: str | None = Field(
        None, description="Path of the reviewed spec, mirroring Plan.source_document."
    )
    structural_findings: list[StructuralFinding] = Field(default_factory=list)
    findings: list[SpecFinding] = Field(default_factory=list)
    questions_for_author: list[str] = Field(
        default_factory=list,
        description="The findings rephrased as the questions a reviewer would send back.",
    )
    readiness_score: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Computed in pure code from the findings (RC1-290). None until scored.",
    )
    verdict: SpecVerdict = Field(
        SpecVerdict.ADVISORY,
        description="Set by code, gating on category. Advisory by default.",
    )

    @property
    def sorted_findings(self) -> list[SpecFinding]:
        """Rubric findings worst-first, stable within a severity."""
        return sorted(self.findings, key=lambda f: f.severity_rank)

    @property
    def blockers(self) -> list[SpecFinding]:
        return [f for f in self.findings if f.severity is SpecSeverity.BLOCKER]
