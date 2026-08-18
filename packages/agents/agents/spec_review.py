"""Spec Review Agent — the rubric half of the Spec Quality Gate (RC1-289).

Reviews a PRD/spec *before* anyone plans against it. Same shape as the other
agents: schema-forced single-shot proposal (`ProposedSpecReview`), Python
stamps the run facts, deterministic code downstream decides everything that
matters (quote verification, readiness score, verdict — RC1-290).

The structural findings from `planner_core.spec_gate.structural` are passed in
as already-recorded context — the model builds on them instead of re-deriving
them, mirroring the PR agent's precomputed-findings pattern.

Bump `RUBRIC_VERSION` on ANY change to the prompts below — the evals attribute
score movement to it, and an unbumped edit makes a regression unattributable.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from planner_core import Provenance
from planner_core.spec_gate import SpecFinding, SpecReview, StructuralFinding
from pydantic import ValidationError

from agents.schema import ProposedProvenance, ProposedSpecFinding, ProposedSpecReview
from agents.usage import AgentUsage

AGENT_NAME = "spec-review"
DEFAULT_MODEL = "claude-sonnet-5"

#: Version of the rubric prompt. Recorded on every SpecReview this agent
#: produces; bump on any prompt change (see module docstring).
RUBRIC_VERSION = 1

SYSTEM_PROMPT = """\
You are a senior technical program manager reviewing a PRD or technical spec \
BEFORE anyone plans against it. The most expensive defect is an ambiguous \
requirement; your review is the gate that catches it while it is still cheap.

Report findings in exactly these six categories:

- ambiguous_quantifier: "fast", "scalable", "soon", "minimal" — and the harder \
case: a number that reads precise but carries no percentile, population, or \
measurement window ("under a second" — at which percentile, under what load, \
measured where?).
- untestable_criterion: no observable pass/fail condition. The test to apply: \
could two competent people disagree about whether this shipped?
- missing_nfr: SLO, rollback, retention, security, accessibility, cost ceiling. \
Weight by what the document's own subject makes conspicuous — a migration spec \
silent on rollback is a worse finding than a UI spec silent on retention. \
Anchor the quote to the text that RAISES the expectation (the cutover sentence, \
the "handles all authentication" claim), since an absence has no text of its own.
- unstated_assumption: the spec depends on something never declared — another \
team's deliverable, a version already deployed, an approval already granted.
- conflicting_requirement: two clauses that cannot both hold. These usually \
live in DIFFERENT sections — read the whole document against itself. Quote one \
clause and name the other, with its section, in your reasoning.
- unowned_scope: a deliverable with no accountable individual. Role-only \
ownership ("the platform team") and passive voice ("materials will be \
produced") both qualify — a team is not a person you can ask.

Severity:
- blocker: planning against this spec produces a plan that cannot survive \
contact — contradictory requirements, or a missing NFR the subject matter makes \
critical (no rollback on a migration).
- warning: a competent reviewer would send the document back over this.
- nit: worth fixing, would not block a review.

Rules:
- Every finding MUST carry provenance: reasoning (why this is a defect), \
source_quote copied VERBATIM from the spec (copy exactly — do not paraphrase), \
source_section (the exact heading the quote sits under, or null), and \
confidence.
- A finding you cannot anchor to a verbatim quote does not exist. Do not \
report it.
- STRUCTURAL FINDINGS listed in the input are already recorded by \
deterministic checks. Do not repeat them; report only what judgment adds on \
top of them.
- suggested_rewrite is a concrete replacement for the quoted text — a \
proposal, never a demand.
- questions_for_author: rephrase the findings as the questions a reviewer \
would actually send back, grouped and deduplicated so the author receives one \
readable message, not one question per finding.
- Do not invent problems. A genuinely good spec yields an empty findings \
list, and that is a valid, complete answer.
- Hold the bar at "a competent reviewer would send it back". A document that \
states measurable targets, named owners, and a rollback path has earned the \
benefit of the doubt: do not flag a neighboring detail it could additionally \
have specified. missing_nfr fires only when the absence is conspicuous given \
the document's subject — never merely because one more NFR could be imagined.
"""


# The agent only needs `client.messages.parse(...)`; typed loosely so a plain
# fake object with that method can be injected in tests without the SDK.
StructuredClient = Any


def _format_structural(findings: Sequence[StructuralFinding]) -> str:
    if not findings:
        return "(none — the deterministic checks found nothing)"
    lines = []
    for f in findings:
        where = f" [{f.section}]" if f.section else ""
        quote = f' — "{f.quote}"' if f.quote else ""
        lines.append(f"- ({f.severity.value}) {f.code}{where}: {f.message}{quote}")
    return "\n".join(lines)


def build_user_prompt(spec_text: str, structural: Sequence[StructuralFinding]) -> str:
    """Assemble the user turn: recorded structural findings, then the spec."""
    return (
        "STRUCTURAL FINDINGS (already recorded by deterministic checks — do not repeat):\n"
        f"{_format_structural(structural)}\n\n"
        "SPEC:\n"
        "-----\n"
        f"{spec_text}\n"
        "-----\n\n"
        "Review this spec against the rubric."
    )


class SpecReviewAgent:
    """Reads a spec, emits a draft `SpecReview` with stamped provenance.

    The draft carries the rubric findings, the questions block, and the
    structural findings passed through; quote verification, the readiness
    score, and the verdict are applied downstream by deterministic code
    (RC1-290) — this agent never decides them.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        client: StructuredClient | None = None,
        now: datetime | None = None,
    ) -> None:
        self._model = model or os.environ.get("LPA_ANTHROPIC_MODEL", DEFAULT_MODEL)
        self._client = client
        self._now = now  # injectable so tests get a deterministic timestamp
        #: Proposed findings dropped because they could not construct a canonical
        #: SpecFinding (e.g. a whitespace-only quote). Silently losing them would
        #: hide a rubric regression, so the count is a first-class output.
        self.last_dropped_invalid = 0

    def run(
        self, spec_text: str, structural: Sequence[StructuralFinding] = ()
    ) -> SpecReview:
        client = self._client or self._default_client()
        proposal = self._propose(client, spec_text, structural)
        return self._stamp(proposal, structural)

    def _default_client(self) -> StructuredClient:
        import anthropic

        return anthropic.Anthropic()

    def _propose(
        self,
        client: StructuredClient,
        spec_text: str,
        structural: Sequence[StructuralFinding],
    ) -> ProposedSpecReview:
        response = client.messages.parse(
            model=self._model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(spec_text, structural)}],
            output_format=ProposedSpecReview,
        )
        self.last_usage = AgentUsage.of(response, self._model)
        return response.parsed_output

    def _stamp(
        self, proposal: ProposedSpecReview, structural: Sequence[StructuralFinding]
    ) -> SpecReview:
        ts = self._now or datetime.now(UTC)
        findings: list[SpecFinding] = []
        self.last_dropped_invalid = 0
        for proposed in proposal.findings:
            try:
                findings.append(self._finding(proposed, ts))
            except ValidationError:
                self.last_dropped_invalid += 1
        return SpecReview(
            structural_findings=list(structural),
            findings=findings,
            questions_for_author=list(proposal.questions_for_author),
            rubric_version=RUBRIC_VERSION,
        )

    def _finding(self, proposed: ProposedSpecFinding, ts: datetime) -> SpecFinding:
        return SpecFinding(
            category=proposed.category,
            severity=proposed.severity,
            suggested_rewrite=proposed.suggested_rewrite,
            provenance=self._provenance(proposed.provenance, ts),
        )

    def _provenance(self, proposed: ProposedProvenance, ts: datetime) -> Provenance:
        return Provenance(
            reasoning=proposed.reasoning,
            source_quote=proposed.source_quote,
            source_section=proposed.source_section,
            confidence=proposed.confidence,
            agent=AGENT_NAME,
            model=self._model,
            timestamp=ts,
        )
