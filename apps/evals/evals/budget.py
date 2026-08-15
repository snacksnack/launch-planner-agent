"""Declared ceilings per subject, and what happens when one is crossed.

RC1-254 asks for cost, latency and token budgets with real numbers attached. The
numbers here are **measured, not guessed** — each ceiling is set from an observed
run with headroom, and the observation is recorded next to it so a future reader
can tell a deliberate limit from a number someone liked the look of.

## A breach is a finding, not an exception

It appears in the same report as the quality findings, because the decision it
informs — *can this subject move to a cheaper model* — is answered by looking at
cost and quality together. Splitting them into two reports is how the cost one
stops being read.

## Breaches are advisory

A run that costs more than expected has not produced a wrong answer, and failing
a build on it would be failing on the weather. RC1-255 gates on correctness;
cost is surfaced, tracked, and left for a human. The one thing a budget must not
do is go unnoticed, and `CharacteristicResult.advisory` already carries exactly
that distinction through the run record.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Ceiling:
    """What a subject is expected to cost, and why that number."""

    subject: str
    max_cost_usd: Decimal
    max_latency_ms: float
    note: str

    def breaches(self, cost: Decimal, latency_ms: float) -> list[str]:
        found = []
        if cost > self.max_cost_usd:
            over = (cost / self.max_cost_usd - 1) * 100 if self.max_cost_usd else Decimal(0)
            found.append(f"cost ${cost} exceeds ${self.max_cost_usd} ceiling by {over:.0f}%")
        if latency_ms > self.max_latency_ms:
            found.append(
                f"latency {latency_ms / 1000:.0f}s exceeds "
                f"{self.max_latency_ms / 1000:.0f}s ceiling"
            )
        return found


#: Measured on 2026-08-15, with headroom. A ceiling set below what a subject
#: actually costs is a ceiling that fires every run and gets ignored; one set far
#: above it never fires at all. Roughly 2x observed is the compromise — enough
#: that ordinary variance passes, tight enough that a doubled prompt or a model
#: change shows up.
CEILINGS: dict[str, Ceiling] = {
    "tool-selection": Ceiling(
        subject="tool-selection",
        max_cost_usd=Decimal("0.50"),
        max_latency_ms=90_000,
        note="observed $0.235 / 35s over 14 cases on claude-sonnet-5",
    ),
    "status-narrative": Ceiling(
        subject="status-narrative",
        max_cost_usd=Decimal("0.15"),
        max_latency_ms=90_000,
        note="observed $0.057 / 40s over 12 cases on claude-sonnet-5",
    ),
    # The free subjects are budgeted too, at zero. A deterministic subject that
    # starts costing money has had a model introduced into it, which is a
    # finding worth surfacing loudly rather than a rounding difference.
    "groundedness": Ceiling(
        subject="groundedness",
        max_cost_usd=Decimal("0"),
        max_latency_ms=5_000,
        note="deterministic — any cost at all means a model crept in",
    ),
    "status-narrative-fallback": Ceiling(
        subject="status-narrative-fallback",
        max_cost_usd=Decimal("0"),
        max_latency_ms=5_000,
        note="deterministic — any cost at all means a model crept in",
    ),
    "health": Ceiling(
        subject="health",
        max_cost_usd=Decimal("0"),
        max_latency_ms=10_000,
        note="deterministic walking skeleton",
    ),
}


def for_subject(subject: str) -> Ceiling | None:
    return CEILINGS.get(subject)
