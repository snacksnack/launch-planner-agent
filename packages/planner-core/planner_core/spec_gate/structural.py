"""Structural checks — the deterministic, free half of the spec gate (RC1-288).

Every check is a pure function over the parsed section list: no LLM, no
network, no config reads, no clock — the same discipline the PR agent's
`checks/n8n.py` holds to, and what keeps the eval suite's free half free
(ADR-0031). Their findings are also injected into the rubric prompt as
already-recorded context (RC1-289), mirroring `format_precomputed_findings`
on the PR agent, so the model builds on them instead of re-deriving them.

**Precision over recall, deliberately.** A structural check that flags a good
spec gets muted, and a muted check catches nothing (the five rounds of
false positives on the groundedness checker bought this rule — see the
agent-evals README). Concretely: checks that judge a section's *content* only
fire when the section exists; unknown document shapes yield no finding rather
than a spurious one; the judgment calls ("is 'the platform team' a real
owner?") belong to the rubric, not here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from planner_core.spec_gate.ingest import SpecSection
from planner_core.spec_gate.models import SpecSeverity, StructuralFinding

#: Keyword → severity when the section is missing. Matched against normalized
#: headings by substring, so "Scope and requirements" satisfies both "scope"
#: and "requirement", and "Goals" satisfies "goal". Non-goals is checked first
#: so its headings are not consumed by the "goal" keyword.
DEFAULT_REQUIRED_SECTIONS: tuple[tuple[str, SpecSeverity], ...] = (
    ("non-goal", SpecSeverity.NIT),
    ("goal", SpecSeverity.WARNING),
    ("requirement", SpecSeverity.WARNING),
    ("acceptance criteria", SpecSeverity.WARNING),
)

_REQUIREMENT_ID = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_UNRESOLVED = re.compile(r"\bTBD\b|\bTODO\b|\?\?\?")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)
#: Two consecutive capitalized words — the name-shaped-token heuristic.
_NAME_TOKEN = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")

#: Sections whose body is commitment-bearing: an unresolved marker in one of
#: these is scope nobody finished writing. Background/appendix sections are
#: deliberately not scanned.
_SCOPE_BEARING = ("goal", "scope", "requirement", "acceptance", "rollout", "timeline", "plan")


def _norm(heading: str | None) -> str:
    return (heading or "").casefold()


def _find(sections: Sequence[SpecSection], keyword: str) -> list[SpecSection]:
    return [s for s in sections if keyword in _norm(s.heading)]


def check_required_sections(
    sections: Sequence[SpecSection],
    required: tuple[tuple[str, SpecSeverity], ...] = DEFAULT_REQUIRED_SECTIONS,
) -> list[StructuralFinding]:
    """Every required section keyword matches at least one heading."""
    findings: list[StructuralFinding] = []
    unclaimed = list(sections)
    for keyword, severity in required:
        matches = [s for s in unclaimed if keyword in _norm(s.heading)]
        if matches:
            # Consume matches so "Non-goals" cannot also satisfy "goal".
            unclaimed = [s for s in unclaimed if s not in matches]
        else:
            findings.append(
                StructuralFinding(
                    code="missing-section",
                    severity=severity,
                    message=f"no section matching {keyword!r} found",
                )
            )
    return findings


def check_requirement_ids(sections: Sequence[SpecSection]) -> list[StructuralFinding]:
    """A requirements section exists but carries no stable requirement IDs.

    Without IDs a plan can only cite requirements by paraphrase. Fires only
    when a requirements section exists — a missing section is already
    `missing-section`, not this.
    """
    requirement_sections = _find(sections, "requirement")
    if not requirement_sections:
        return []
    if any(_REQUIREMENT_ID.search(s.prose_body) for s in requirement_sections):
        return []
    section = requirement_sections[0]
    return [
        StructuralFinding(
            code="missing-requirement-ids",
            severity=SpecSeverity.WARNING,
            message="requirements carry no stable IDs (e.g. REQ-1); "
            "a plan can only cite them by paraphrase",
            section=section.heading,
        )
    ]


def check_named_owner(sections: Sequence[SpecSection]) -> list[StructuralFinding]:
    """An ownership section exists but names no individual anywhere.

    Deterministic to the extent of "is there a name-shaped token" (two
    consecutive capitalized words). Whether a named team counts as a real
    owner is the rubric's judgment call (`unowned_scope`), not this check's.
    """
    owner_sections = _find(sections, "owner")
    if not owner_sections:
        return []
    if any(_NAME_TOKEN.search(s.prose_body) for s in owner_sections):
        return []
    section = owner_sections[0]
    return [
        StructuralFinding(
            code="no-named-owner",
            severity=SpecSeverity.WARNING,
            message="the ownership section names no individual — "
            "there is no accountable person to ask",
            section=section.heading,
        )
    ]


def check_countable_criteria(sections: Sequence[SpecSection]) -> list[StructuralFinding]:
    """An acceptance-criteria section exists but is prose, not discrete items."""
    criteria_sections = _find(sections, "acceptance criteria")
    findings: list[StructuralFinding] = []
    for section in criteria_sections:
        body = section.prose_body
        if body.strip() and not _LIST_ITEM.search(body):
            findings.append(
                StructuralFinding(
                    code="uncountable-criteria",
                    severity=SpecSeverity.WARNING,
                    message="acceptance criteria are a paragraph, not countable items — "
                    "nothing can be checked off",
                    section=section.heading,
                )
            )
    return findings


def check_unresolved_markers(sections: Sequence[SpecSection]) -> list[StructuralFinding]:
    """TBD / TODO / ??? left in a scope-bearing section.

    Scans `prose_body` only — a TODO inside a fenced code sample is code, not
    unfinished scope.
    """
    findings: list[StructuralFinding] = []
    for section in sections:
        if not any(k in _norm(section.heading) for k in _SCOPE_BEARING):
            continue
        for line in section.prose_body.splitlines():
            if _UNRESOLVED.search(line):
                findings.append(
                    StructuralFinding(
                        code="unresolved-marker",
                        severity=SpecSeverity.WARNING,
                        message="unresolved marker left in a scope-bearing section",
                        quote=line.strip(),
                        section=section.heading,
                    )
                )
    return findings


def run_structural_checks(
    sections: Sequence[SpecSection],
    required: tuple[tuple[str, SpecSeverity], ...] = DEFAULT_REQUIRED_SECTIONS,
) -> list[StructuralFinding]:
    """Run every check in a fixed order — output is deterministic by construction."""
    return [
        *check_required_sections(sections, required),
        *check_requirement_ids(sections),
        *check_named_owner(sections),
        *check_countable_criteria(sections),
        *check_unresolved_markers(sections),
    ]
