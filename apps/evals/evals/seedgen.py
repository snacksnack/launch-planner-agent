"""Generating the seed set — and why it deliberately contains bad outputs.

A calibration set of uniformly good outputs measures nothing. Both scorers give
everything a 2, the expected agreement is 1, kappa is undefined, and the honest
conclusion is "there was no variance here" rather than "the judge is perfect"
(`agreement.py` says exactly that when it happens). So the set has to span the
range the judge will actually meet.

Three variants over the same facts give that spread without hand-writing prose:

* `agent` — the shipped `StatusAgent`. What production emits.
* `fallback` — `planner_core.fallback_narrative`. Rule-written, so it is
  perfectly grounded and complete by construction, and noticeably flatter on
  tone and actionability. It anchors the "accurate but dull" corner that the
  rubric explicitly says should still score 2 on groundedness.
* `degraded` — the same facts through a deliberately vague prompt that invites
  padding and unsupported causal claims. It supplies the low end.

The facts themselves vary too: a quiet week, a bad week, a missed deadline, and
the no-baseline case where "nothing to compare" and "nothing changed" mean
opposite things.

Generation is billed and run once; the output is committed. Nobody should need
an API key to *label* a seed set, only to create one.
"""

from __future__ import annotations

from datetime import date

from agent_evals.seeds import Seed
from agents.status import DEFAULT_MODEL, StatusAgent, build_user_prompt
from app.config import get_settings
from planner_core import StatusFacts, StatusNarrative, fallback_narrative
from planner_core.status import Breach, Health, MilestoneDrift, NamedChange, RaidChange

SUBJECT = "status-narrative"

#: A prompt that asks for the same artifact but invites everything the rubric
#: penalises: padding, hedging, and causal claims the facts do not support.
#: Present so the low end of the scale is populated by something a real prompt
#: edit could plausibly produce — not by nonsense a judge would never see.
DEGRADED_SYSTEM_PROMPT = """\
You are writing a weekly project status update.

Write an exec_summary and some points. Be reassuring and diplomatic. Add context \
about why things happened and how the team is feeling about them, so leadership \
has the full picture. It is fine to be expansive.
"""


def _task_id(name: str) -> str:
    return "task-" + name.lower().replace(" ", "-")


def _facts(
    label: str,
    health: Health,
    reasons: list[str],
    *,
    before: date | None,
    after: date | None,
    shift: int,
    slipped: list[tuple[str, int]] = (),
    newly_critical: list[str] = (),
    no_longer_critical: list[str] = (),
    breaches: list[tuple[str, str, int]] = (),
    milestones: list[tuple[str, date, date, int]] = (),
    raid_added: list[tuple[str, str, str, int]] = (),
    raid_removed: list[tuple[str, str, str, int]] = (),
    baseline_version: int | None = 1,
) -> StatusFacts:
    return StatusFacts(
        period_label=label,
        baseline_version=baseline_version,
        health=health,
        health_reasons=reasons,
        launch_before=before,
        launch_after=after,
        launch_shift_days=shift,
        slipped=[NamedChange(id=_task_id(n), name=n, shift_days=d) for n, d in slipped],
        newly_critical=[NamedChange(id=_task_id(n), name=n) for n in newly_critical],
        no_longer_critical=[NamedChange(id=_task_id(n), name=n) for n in no_longer_critical],
        breaches=[
            Breach(constraint_id=c, task_id=_task_id(t), slack_days=s) for c, t, s in breaches
        ],
        milestone_drift=[
            MilestoneDrift(
                id=_task_id(n),
                name=n,
                projected_before=b,
                projected_after=a,
                slack_shift_days=slack,
            )
            for n, b, a, slack in milestones
        ],
        raid_added=[
            RaidChange(id=i, type=t, title=title, severity=sev) for i, t, title, sev in raid_added
        ],
        raid_removed=[
            RaidChange(id=i, type=t, title=title, severity=sev) for i, t, title, sev in raid_removed
        ],
        structural_change_count=len(slipped) + len(newly_critical) + len(no_longer_critical),
    )


#: Deliberately spans quiet-to-bad, plus the two edges that mean the opposite of
#: what a careless narrative would say.
FACT_SETS: tuple[StatusFacts, ...] = (
    _facts(
        "Week of 2026-08-03",
        Health.GREEN,
        ["no launch movement", "no new critical work"],
        before=date(2026, 10, 12),
        after=date(2026, 10, 12),
        shift=0,
    ),
    _facts(
        "Week of 2026-08-10",
        Health.YELLOW,
        ["launch slipped 4 working days"],
        before=date(2026, 10, 12),
        after=date(2026, 10, 16),
        shift=4,
        slipped=[("Legal sign-off", 4)],
    ),
    _facts(
        "Week of 2026-08-17",
        Health.RED,
        ["launch slipped 24 working days", "2 tasks newly critical"],
        before=date(2026, 10, 12),
        after=date(2026, 11, 13),
        shift=24,
        slipped=[("Legal sign-off", 30), ("Vendor contract", 6)],
        newly_critical=["Security review", "Data migration rehearsal"],
    ),
    _facts(
        "Week of 2026-08-24",
        Health.GREEN,
        ["launch pulled in 2 working days"],
        before=date(2026, 10, 16),
        after=date(2026, 10, 14),
        shift=-2,
    ),
    # The edge that matters: no baseline to compare against. "Nothing to compare"
    # and "nothing changed" mean opposite things, and a narrative that conflates
    # them is the failure RC1-252 regression-tests for the digest.
    _facts(
        "Week of 2026-07-27",
        Health.GREEN,
        ["no baseline committed yet"],
        before=None,
        after=date(2026, 10, 12),
        shift=0,
        baseline_version=None,
    ),
    _facts(
        "Week of 2026-08-31",
        Health.YELLOW,
        ["1 task newly critical"],
        before=date(2026, 10, 14),
        after=date(2026, 10, 14),
        shift=0,
        newly_critical=["Cutover rehearsal"],
    ),
    # A missed hard deadline. Completeness has a clear right answer here: an
    # update that does not mention the breach is missing the most important fact
    # of the week, whatever else it says.
    _facts(
        "Week of 2026-09-07",
        Health.RED,
        ["hard deadline missed by 5 working days"],
        before=date(2026, 10, 14),
        after=date(2026, 10, 21),
        shift=5,
        slipped=[("Compliance sign-off", 5)],
        breaches=[("constraint-regulatory-cutoff", "Compliance sign-off", -5)],
    ),
    # Milestone drift with no launch movement — the interesting case where the
    # headline number is unchanged but something real moved underneath it.
    _facts(
        "Week of 2026-09-14",
        Health.YELLOW,
        ["milestone slack consumed"],
        before=date(2026, 10, 21),
        after=date(2026, 10, 21),
        shift=0,
        milestones=[("Beta cutover", date(2026, 9, 28), date(2026, 10, 5), -5)],
    ),
    # RAID churn only. Nothing about the schedule changed.
    _facts(
        "Week of 2026-09-21",
        Health.YELLOW,
        ["1 new high-severity risk"],
        before=date(2026, 10, 21),
        after=date(2026, 10, 21),
        shift=0,
        raid_added=[("risk-vendor-capacity", "risk", "Vendor cannot staff the cutover weekend", 4)],
        raid_removed=[("issue-test-data", "issue", "Test data refresh was blocked", 2)],
    ),
    # A genuinely quiet week: nothing at all. The temptation is to write
    # something anyway, and a narrative that manufactures activity here is the
    # failure the empty-findings case covers for the drift digest.
    _facts(
        "Week of 2026-09-28",
        Health.GREEN,
        ["no changes this period"],
        before=date(2026, 10, 21),
        after=date(2026, 10, 21),
        shift=0,
    ),
    # Recovery: work coming *off* the critical path. Easy to narrate as though
    # it were bad news out of habit.
    _facts(
        "Week of 2026-10-05",
        Health.GREEN,
        ["launch pulled in 3 working days", "2 tasks no longer critical"],
        before=date(2026, 10, 21),
        after=date(2026, 10, 16),
        shift=-3,
        no_longer_critical=["Security review", "Vendor contract"],
    ),
    # Many small slips that individually look harmless and together are not.
    _facts(
        "Week of 2026-10-12",
        Health.YELLOW,
        ["4 tasks slipped", "launch slipped 2 working days"],
        before=date(2026, 10, 16),
        after=date(2026, 10, 20),
        shift=2,
        slipped=[
            ("Runbook review", 1),
            ("Load testing", 2),
            ("DNS cutover plan", 1),
            ("Rollback rehearsal", 3),
        ],
    ),
)


def _seed(
    index: int, variant: str, facts: StatusFacts, narrative: StatusNarrative, version: str
) -> Seed:
    return Seed(
        id=f"{SUBJECT}-{index:02d}-{variant}",
        subject=SUBJECT,
        variant=variant,
        facts=facts.model_dump(mode="json"),
        output=narrative.model_dump(mode="json"),
        generator_version=version,
    )


def preflight() -> None:
    """Generation is billed; labelling is not. Fail before spending anything."""
    if not get_settings().anthropic_api_key:
        raise RuntimeError(
            "LPA_ANTHROPIC_API_KEY is not set. Generating the seed set drives a real model. "
            "Labelling an already-generated set needs no credentials — that is the point of "
            "committing it (ADR-0033)."
        )


def _default_client():
    """Built from `LPA_ANTHROPIC_API_KEY`, not the bare `anthropic.Anthropic()`
    that `StatusAgent._default_client` falls back to — that one reads
    `ANTHROPIC_API_KEY` and would miss this repo's prefixed setting entirely."""
    import anthropic

    preflight()
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)


def generate(client=None, model: str = DEFAULT_MODEL) -> list[Seed]:
    """Produce the full seed set. Billed for the two LLM variants.

    `client` is injectable so the tests can drive the whole path with a fake —
    the same seam `agents` uses, and the reason `uv run pytest` stays
    credential-free.
    """
    client = client or _default_client()
    seeds: list[Seed] = []
    for index, facts in enumerate(FACT_SETS):
        # Free, and anchors the accurate-but-flat corner of the rubric.
        seeds.append(_seed(index, "fallback", facts, fallback_narrative(facts), "planner_core"))

        agent = StatusAgent(model=model, client=client)
        seeds.append(_seed(index, "agent", facts, agent.run(facts), model))

        seeds.append(
            _seed(index, "degraded", facts, _degraded(client, model, facts), f"{model}+degraded")
        )
    return seeds


def _degraded(client, model: str, facts: StatusFacts) -> StatusNarrative:
    """Same schema, same facts, a prompt that invites what the rubric penalises.

    Goes through `messages.parse` exactly as the real agent does, so the only
    difference between this and `agent` is the system prompt — which is what
    makes the pair informative rather than just noisy.
    """
    response = client.messages.parse(
        model=model,
        max_tokens=1024,
        system=DEGRADED_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(facts)}],
        output_format=StatusNarrative,
    )
    return response.parsed_output
