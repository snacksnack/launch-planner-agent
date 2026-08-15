"""Groundedness — measured deterministically first, judged only where it must be.

The failure that matters in a narrative subject is a claim with no support in the
input: an invented ticket key, a date that was never in the payload, a severity
quietly softened. Most of those are **exactly checkable**, and checking them
costs nothing:

* every ticket key in the output must appear in the input
* every date in the output must appear in the input
* every day-count must appear in the input
* the output must not assert a health state the facts contradict

None of that needs a model, and none of it is a matter of opinion. RC1-250
measured the judge at weighted kappa 0.66 with a third of its interval below the
gating floor, so groundedness is **advisory** there — the deterministic layer is
what can actually be trusted to fail a build, and it is the layer that catches the
most embarrassing failures anyway.

## Precision over recall, deliberately

A checker that flags correct output is worse than no checker: it gets muted, and
then it catches nothing. So every rule here is chosen to be *conservative* —
narrow patterns, normalised comparison, and an explicit decision not to check
free-floating numbers, which appear in prose in too many forms to distinguish a
hallucination from a rounding. Measured against the 36 real seeds in
`apps/evals/calibration`, this layer must produce **zero** false positives on the
`fallback` variant, which is a template over the facts and cannot be ungrounded.
`test_groundedness.py` asserts exactly that.

What the layer cannot check — "the team has absorbed the slip", "reflecting
improved schedule buffer" — is left to the judge, and the judge's verdict is
advisory until RC1-260 resolves the rubric ambiguity that caps it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: `RC1-123`, `SKY-45`. Deliberately requires an uppercase prefix: task ids in
#: the facts look like `task-legal-sign-off`, and matching those would flag
#: every correctly-quoted task name as a fabricated key.
_TICKET_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}
#: `October 12, 2026` and `October 12`. The year is optional because narratives
#: routinely drop it, and a month/day that matches a fact date is support enough.
_PROSE_DATE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(\d{4}))?\b",
    re.IGNORECASE,
)

#: `24 working days`, `3 day(s)`. Anchored to the unit on purpose — a bare
#: integer in prose ("two tasks", "the third week") is not a claim this layer can
#: adjudicate, and guessing would cost precision for very little recall.
_DAY_COUNT = re.compile(r"\b(\d{1,3})\s+(?:working\s+)?days?\b", re.IGNORECASE)

#: Phrases that assert a health state — but only when they are asserted *about
#: the whole plan*. See `_health_contradictions` for why that qualifier is the
#: entire difference between a useful check and a muted one.
_HEALTH_PHRASES: dict[str, tuple[str, ...]] = {
    "green": ("on track", "green", "healthy", "no risk", "tracking well"),
    "yellow": ("at risk", "at some risk", "yellow", "needs attention", "some concern"),
    "red": ("off track", "red", "critical risk", "in trouble"),
}

#: What the claim has to be *about*. A narrative says plenty of true things
#: about parts of the work — "core engineering remains on track" is not a claim
#: about overall health, and flagging it would be wrong.
_HEALTH_SUBJECT = re.compile(
    r"\b(?:the\s+)?(?:overall\s+)?(?:project|program|programme|plan|delivery|launch|"
    r"status|health|week)\b",
    re.IGNORECASE,
)

#: A state word inside one of these is not an assertion of that state.
#: "no red flags" is a *green* sentence containing the word "red".
_NEGATORS = ("no ", "not ", "never ", "without ", "avoid", "n't ")

#: Sentence-ish boundary. A subject in one clause does not govern a state word
#: three sentences later.
_CLAUSE_WINDOW = 80


@dataclass(frozen=True)
class Violation:
    """One unsupported or contradicted claim."""

    kind: str
    value: str
    detail: str


@dataclass
class Report:
    """What the deterministic layer found."""

    checked: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return not self.violations

    @property
    def hallucination_rate(self) -> float:
        """Unsupported fraction of the claims this layer could check.

        A rate rather than a boolean, because RC1-251 asks for it per subject
        per run: one invented key in a long narrative and a narrative that is
        entirely invention are different findings.
        """
        return len(self.violations) / self.checked if self.checked else 0.0

    def summary(self) -> str:
        if self.grounded:
            return f"{self.checked} checkable claim(s), all supported"
        first = self.violations[0]
        more = f" (+{len(self.violations) - 1} more)" if len(self.violations) > 1 else ""
        return f"{first.kind}: {first.detail}{more}"


def _fact_strings(facts: Any) -> set[str]:
    """Every scalar in the facts, as a string. The corpus support is checked
    against — flattened rather than path-aware, because a claim is supported if
    the value appears *anywhere* in the input, not at a particular key."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list | tuple):
            for value in node:
                walk(value)
        elif node is not None and not isinstance(node, bool):
            found.add(str(node))

    walk(facts)
    return found


def _fact_dates(strings: set[str]) -> set[date]:
    """Dates anywhere inside a fact value, not only whole-field values.

    `finditer`, not `fullmatch`. The first version matched whole values only and
    therefore missed `period_label: "Week of 2026-08-03"` — so a narrative
    correctly writing "August 3, 2026" was flagged as inventing it. Caught by
    running the checker over the 36 real seeds and reading a flag that looked
    wrong, which is the only reason it did not ship.
    """
    dates = set()
    for value in strings:
        for match in _ISO_DATE.finditer(value):
            year, month, day = (int(part) for part in match.groups())
            try:
                dates.add(date(year, month, day))
            except ValueError:
                continue
    return dates


def _fact_integers(strings: set[str]) -> set[int]:
    """Absolute values: a narrative writes "pulled in 3 days" where the fact is
    `launch_shift_days: -3`. The sign is carried by the prose, not the number."""
    out = set()
    for value in strings:
        try:
            out.add(abs(int(float(value))))
        except (TypeError, ValueError):
            continue
    return out


def check(output: str, facts: Any) -> Report:
    """Check one output against the structured input it was written from."""
    strings = _fact_strings(facts)
    corpus = " ".join(strings).lower()
    fact_dates = _fact_dates(strings)
    fact_integers = _fact_integers(strings)
    report = Report()

    for key in set(_TICKET_KEY.findall(output)):
        report.checked += 1
        if key not in strings and key.lower() not in corpus:
            report.violations.append(
                Violation("invented_ticket_key", key, f"{key} does not appear in the facts")
            )

    for match in _ISO_DATE.finditer(output):
        report.checked += 1
        year, month, day = (int(part) for part in match.groups())
        try:
            found = date(year, month, day)
        except ValueError:
            report.violations.append(
                Violation("impossible_date", match.group(0), f"{match.group(0)} is not a date")
            )
            continue
        if found not in fact_dates:
            report.violations.append(
                Violation("invented_date", match.group(0), f"{found} does not appear in the facts")
            )

    for match in _PROSE_DATE.finditer(output):
        month = _MONTHS[match.group(1).lower()]
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else None
        report.checked += 1
        candidates = (
            {d for d in fact_dates if d.month == month and d.day == day}
            if year is None
            else {d for d in fact_dates if (d.year, d.month, d.day) == (year, month, day)}
        )
        if not candidates:
            report.violations.append(
                Violation(
                    "invented_date",
                    match.group(0),
                    f"{match.group(0)} does not appear in the facts",
                )
            )

    for match in _DAY_COUNT.finditer(output):
        value = int(match.group(1))
        report.checked += 1
        if value not in fact_integers:
            report.violations.append(
                Violation(
                    "invented_day_count",
                    match.group(0),
                    f"{match.group(0)!r} — {value} is not a number in the facts",
                )
            )

    report.violations.extend(_health_contradictions(output, facts))
    if isinstance(facts, dict) and facts.get("health"):
        report.checked += 1
    return report


def _health_contradictions(output: str, facts: Any) -> list[Violation]:
    """The must-not-say that can be derived rather than declared.

    A narrative may not assert a health state the facts contradict — softening a
    red week to "at some risk" is the failure RC1-251 names, and it is a
    violation *even when every number in the output is correct*. The drift
    digest prompt makes the same point from the other side: the model does not
    compute severity, so an output that editorialises it is wrong by
    construction.

    Three rules, each bought by a false positive on real output. A keyword match
    alone flagged three of five real cases wrongly, and a check that cries wolf
    at that rate gets muted — at which point it catches nothing:

    * **The claim must be about the plan.** "core engineering and product work
      remains on track" is true and says nothing about overall health.
    * **Negation flips it.** "health remains green, with no red flags" is a
      green sentence containing the word "red".
    * **The state word must be near its subject.** A subject in one clause does
      not govern a word three sentences later.

    The cost is recall: an assertion phrased without a nearby subject slips
    through to the judge. That is the right trade for a check meant to gate.
    """
    if not isinstance(facts, dict):
        return []
    actual = str(facts.get("health") or "").lower()
    if actual not in _HEALTH_PHRASES:
        return []

    lowered = output.lower()
    violations = []
    for state, phrases in _HEALTH_PHRASES.items():
        if state == actual:
            continue
        for phrase in phrases:
            if _asserted_about_the_plan(lowered, phrase):
                violations.append(
                    Violation(
                        "contradicted_health",
                        phrase,
                        f"facts say health is {actual!r}, output asserts {phrase!r}",
                    )
                )
                break
    return violations


def _asserted_about_the_plan(lowered: str, phrase: str) -> bool:
    """Is `phrase` claimed about the plan as a whole, un-negated?"""
    for match in re.finditer(rf"\b{re.escape(phrase)}\b", lowered):
        window = lowered[max(0, match.start() - _CLAUSE_WINDOW) : match.start()]
        # Stop at a sentence boundary — a subject in the previous sentence is
        # not the subject of this claim.
        window = re.split(r"[.;]\s", window)[-1]
        if not _HEALTH_SUBJECT.search(window):
            continue
        if any(negator in window[-25:] for negator in _NEGATORS):
            continue
        return True
    return False


def must_not_say(output: str, forbidden: list[str]) -> list[Violation]:
    """Declared must-not-say, alongside the derived health check.

    A first-class expectation rather than an afterthought: RC1-251 requires it
    beside must-say, because an output can state only true things and still
    violate its brief — editorialising severity the model was told not to decide
    is the example the drift digest prompt calls out.
    """
    lowered = output.lower()
    return [
        Violation("said_forbidden_thing", phrase, f"output contains {phrase!r}, which it must not")
        for phrase in forbidden
        if phrase.lower() in lowered
    ]


def rate_across(reports: list[Report]) -> float:
    """Hallucination rate over a whole run, weighted by claims rather than by
    output — a long narrative with one invention should not be averaged against
    a one-line all-clear as though the two carried equal evidence."""
    checked = sum(r.checked for r in reports)
    violations = sum(len(r.violations) for r in reports)
    return violations / checked if checked else 0.0


def describe(facts: Any) -> str:
    """The facts as the judge and a reader both see them."""
    return json.dumps(facts, indent=2, sort_keys=True)
