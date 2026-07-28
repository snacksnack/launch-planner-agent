"""Status Agent — turns the deterministic status facts into exec-ready prose.

The agent's scope is deliberately narrow: it phrases, it never decides. The
health indicator and every fact are computed in `planner_core.status`; the agent
receives only those facts and writes the executive summary and the "what changed
since last week" bullets from them — so every statement maps back to a diff entry
rather than to model imagination. When no credentials are configured, the caller
falls back to `planner_core.fallback_narrative`, so the report always renders.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from planner_core import StatusFacts, StatusNarrative

AGENT_NAME = "status"
DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You are a technical program manager writing a weekly executive status update.

You are given a set of FACTS computed deterministically from the plan: a health \
indicator, the launch-date movement against baseline, slipped tasks, newly \
critical tasks, milestone drift, missed deadlines, and RAID changes.

Write:
- exec_summary: 2-3 plain-English sentences a busy executive can read in ten \
seconds. Lead with the health and the launch-date story.
- points: the "what changed since last week" bullets.

Rules:
- Use ONLY the provided facts. Do NOT invent progress, status, names, or numbers \
that are not in the facts. Every bullet must correspond to a fact.
- Do not contradict the health indicator — it is computed by rule, not your call.
- Be concise and specific; prefer the real task/milestone names and day counts.
"""

# The agent only needs `client.messages.parse(...)`.
StructuredClient = Any


def _fmt_date(d: date | None) -> str:
    return d.isoformat() if d else "n/a"


def build_user_prompt(facts: StatusFacts) -> str:
    """Render the deterministic facts as the agent's sole source material."""
    lines = [
        f"PERIOD: {facts.period_label}",
        f"HEALTH: {facts.health.value} ({'; '.join(facts.health_reasons)})",
        f"LAUNCH: {_fmt_date(facts.launch_before)} -> {_fmt_date(facts.launch_after)} "
        f"({facts.launch_shift_days:+d} working days)",
        f"STRUCTURAL CHANGES: {facts.structural_change_count}",
    ]
    if facts.breaches:
        lines.append("MISSED DEADLINES:")
        lines += [
            f"- {b.constraint_id} on {b.task_id} ({b.slack_days:+d}d)" for b in facts.breaches
        ]
    if facts.slipped:
        lines.append("SLIPPED TASKS:")
        lines += [f"- {s.name} ({s.shift_days:+d}d)" for s in facts.slipped]
    if facts.newly_critical:
        lines.append("NEWLY CRITICAL:")
        lines += [f"- {n.name}" for n in facts.newly_critical]
    if facts.milestone_drift:
        lines.append("MILESTONE DRIFT:")
        lines += [
            f"- {m.name}: {_fmt_date(m.projected_before)} -> {_fmt_date(m.projected_after)}"
            for m in facts.milestone_drift
        ]
    if facts.raid_added:
        lines.append("NEW RAID ITEMS:")
        lines += [f"- {r.type}: {r.title}" for r in facts.raid_added]
    lines.append("\nWrite the executive status update from these facts only.")
    return "\n".join(lines)


class StatusAgent:
    """Writes the exec summary + 'what changed' narrative from the status facts."""

    def __init__(
        self, *, model: str | None = None, client: StructuredClient | None = None
    ) -> None:
        self._model = model or os.environ.get("LPA_ANTHROPIC_MODEL", DEFAULT_MODEL)
        self._client = client

    def run(self, facts: StatusFacts) -> StatusNarrative:
        client = self._client or self._default_client()
        response = client.messages.parse(
            model=self._model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(facts)}],
            output_format=StatusNarrative,
        )
        return response.parsed_output

    def _default_client(self) -> StructuredClient:
        import anthropic

        return anthropic.Anthropic()
