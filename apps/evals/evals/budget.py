"""What each subject in *this* repo is expected to cost.

`Ceiling` and the breach formatting live in `agent_evals.budget`. What stays
here is the part no library could supply: the numbers. A ceiling is a claim
about a specific subject on a specific model, measured on a specific day — it
belongs with the thing it constrains, and a shared package holding it would be
asserting a limit on code it has never seen (RC1-261).

See `agent_evals.budget` for why a breach is advisory rather than a build
failure.
"""

from __future__ import annotations

from decimal import Decimal

from agent_evals.budget import Ceiling, breaches_for

__all__ = ["CEILINGS", "Ceiling", "breaches_for", "for_subject"]

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
    # RC1-257. Measured on 2026-08-15 across the runs that built these subjects.
    # These are the slowest in the suite by a wide margin — the flagship PRD is
    # 101 lines and the agents emit 16k-token structured output, so a minute-plus
    # per case is the shape of the work rather than a problem to tune.
    "work-breakdown": Ceiling(
        subject="work-breakdown",
        max_cost_usd=Decimal("0.60"),
        max_latency_ms=300_000,
        note="observed $0.276 / 127s over 3 cases on claude-sonnet-5",
    ),
    "dependency": Ceiling(
        subject="dependency",
        max_cost_usd=Decimal("0.50"),
        max_latency_ms=240_000,
        note="observed $0.227 / 111s over 2 cases on claude-sonnet-5",
    ),
    "raid": Ceiling(
        subject="raid",
        max_cost_usd=Decimal("0.40"),
        max_latency_ms=240_000,
        note="observed $0.192 / 105s over 2 cases on claude-sonnet-5",
    ),
    "health": Ceiling(
        subject="health",
        max_cost_usd=Decimal("0"),
        max_latency_ms=10_000,
        note="deterministic walking skeleton",
    ),
    # RC1-292.
    "spec-structural": Ceiling(
        subject="spec-structural",
        max_cost_usd=Decimal("0"),
        max_latency_ms=5_000,
        note="deterministic — any cost at all means a model crept in",
    ),
    "spec-review": Ceiling(
        subject="spec-review",
        max_cost_usd=Decimal("0.60"),
        max_latency_ms=240_000,
        note="observed $0.278 / 172s over 2 cases on claude-sonnet-5",
    ),
}


def for_subject(subject: str) -> Ceiling | None:
    return CEILINGS.get(subject)
