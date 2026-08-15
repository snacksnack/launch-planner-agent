"""Shared inputs and structural checks for the three planning agents (RC1-257).

`work_breakdown`, `dependency` and `raid` read the same PRDs against the same
roster, so the corpus and the checks that apply to more than one of them live
here rather than being copied three times.

## Structure is checked separately from judgement, and never merged

The ticket asks for this explicitly, and it matters more here than anywhere else
in the harness. A breakdown can be structurally perfect and useless, or messy and
insightful. Averaging the two produces a number that moves for two unrelated
reasons and tells you nothing about either.

Everything in this module is **deterministic and free**. Provenance tracing,
orphan detection, cycle detection, roster membership — none needs a model, and
all of it catches the failures that most obviously invalidate a plan.

## What the schema catches before we do

`ThreePointEstimate` rejects `optimistic > pessimistic` and `Dependency` rejects
a self-edge, both at parse time. So those failures surface as a **case error**
(exit 2) rather than a failed characteristic (exit 1) — the agent produced
nothing scoreable. That is the honest reporting: there is no partially-valid
breakdown to grade. It also means the schema is the cheapest gate in the suite
and it runs first, for free.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import cache
from pathlib import Path

from agent_evals.record import CharacteristicResult
from planner_core import Epic, Task, TeamMember
from planner_core.validation import normalize_for_quote_match

#: PRDs authored for the evals, alongside the two shipped fixtures. Kept here
#: rather than in `fixtures/` because nothing but the eval reads them, and a
#: fixture directory that mixes "the product's golden" with "input designed to
#: make an agent misbehave" invites someone to treat the second as the first.
PRD_DIR = Path(__file__).resolve().parents[1] / "prds"
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures"


@cache
def prd_text(name: str) -> str:
    """The PRD for a case id. Shipped fixtures by name, eval PRDs by filename."""
    fixture = FIXTURE_DIR / name / "prd.md"
    if fixture.exists():
        return fixture.read_text()
    local = PRD_DIR / f"{name}.md"
    if local.exists():
        return local.read_text()
    raise FileNotFoundError(f"no PRD for case {name!r} in {FIXTURE_DIR} or {PRD_DIR}")


@cache
def team(name: str) -> tuple[TeamMember, ...]:
    """The roster for a case.

    The eval-only PRDs borrow the migration roster rather than inventing one:
    the point of those cases is restraint and risk sensitivity, and a second
    roster would add a variable that has nothing to do with either.
    """
    path = FIXTURE_DIR / name / "team.json"
    if not path.exists():
        path = FIXTURE_DIR / "jira-cloud-migration" / "team.json"
    return tuple(TeamMember(**m) for m in json.loads(path.read_text()))


# --- structural checks, all free ------------------------------------------


def traces_to_the_prd(
    epics: Sequence[Epic], tasks: Sequence[Task], source_text: str
) -> CharacteristicResult:
    """Every epic and task cites a quote that is verbatim in the PRD.

    The same failure class as an invented ticket key in RC1-251: work proposed
    from nothing.

    Matching is delegated to `planner_core.validation.normalize_for_quote_match`
    rather than re-derived here, because getting that normalisation subtly wrong
    is how a checker starts crying wolf — and this one already did. The first
    run flagged three faithful quotes on the shipped `product-launch` PRD, all
    because the PRD wraps them in `**` and the agent quotes what a reader sees.
    Fixing it in `planner_core` fixed the shipped validator at the same time.
    """
    haystack = normalize_for_quote_match(source_text)
    unverifiable = []
    for kind, items in (("epic", epics), ("task", tasks)):
        for item in items:
            quote = normalize_for_quote_match(item.provenance.source_quote or "")
            if not quote or quote not in haystack:
                unverifiable.append(f"{kind} {item.id!r}")
    total = len(epics) + len(tasks)
    return CharacteristicResult(
        name="traces-to-the-prd",
        passed=not unverifiable,
        detail=(
            f"all {total} item(s) cite the PRD verbatim"
            if not unverifiable
            else f"{len(unverifiable)}/{total} cite text not in the PRD: "
            + ", ".join(unverifiable[:4])
        ),
    )


def no_orphan_tasks(epics: Sequence[Epic], tasks: Sequence[Task]) -> CharacteristicResult:
    """Every task's `epic_id` names an epic that exists.

    `epic_id` is optional in the schema, so a null is allowed and not counted —
    the failure is a task pointing at an epic that was never proposed, which is
    a dangling reference rather than a deliberate omission.
    """
    known = {e.id for e in epics}
    dangling = [t.id for t in tasks if t.epic_id is not None and t.epic_id not in known]
    unparented = sum(1 for t in tasks if t.epic_id is None)
    return CharacteristicResult(
        name="no-orphan-tasks",
        passed=not dangling,
        detail=(
            f"every task with an epic_id resolves ({unparented} deliberately unparented)"
            if not dangling
            else f"{len(dangling)} task(s) reference a missing epic: {', '.join(dangling[:4])}"
        ),
    )


def no_duplicate_ids(epics: Sequence[Epic], tasks: Sequence[Task]) -> CharacteristicResult:
    """Ids are unique within each kind, and epic names are not repeated.

    Duplicate ids break every downstream lookup. Duplicate epic *names* under
    distinct ids are the subtler failure — the same work proposed twice, which
    reads as a bigger plan rather than a broken one.
    """
    problems = []
    for kind, ids in (("epic", [e.id for e in epics]), ("task", [t.id for t in tasks])):
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            problems.append(f"duplicate {kind} id(s): {', '.join(repeated)}")
    names = [normalize_for_quote_match(e.name).lower() for e in epics]
    repeated_names = sorted({n for n in names if names.count(n) > 1})
    if repeated_names:
        problems.append(f"duplicate epic name(s): {', '.join(repeated_names)}")
    return CharacteristicResult(
        name="no-duplicate-ids",
        passed=not problems,
        detail="; ".join(problems) if problems else "ids unique, no repeated epic names",
    )


def owners_are_on_the_roster(
    tasks: Sequence[Task], roster: Sequence[TeamMember]
) -> CharacteristicResult:
    """`owner_id` is a real roster id, or null.

    The prompt says "never invent an owner", and null is the sanctioned way to
    say "unclear". An invented id is the failure; an honest null is not.
    """
    known = {m.id for m in roster}
    invented = sorted({t.owner_id for t in tasks if t.owner_id and t.owner_id not in known})
    unassigned = sum(1 for t in tasks if t.owner_id is None)
    return CharacteristicResult(
        name="owners-are-on-the-roster",
        passed=not invented,
        detail=(
            f"every owner is on the roster ({unassigned} left unassigned)"
            if not invented
            else f"invented owner id(s): {', '.join(invented)}"
        ),
    )
