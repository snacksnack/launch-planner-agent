"""Response shapes shared across the plan tools.

`PlanRef` rides on every plan-tool response: a plan of record moves, so a date
is only meaningful next to the version it was computed from. Keeping it here
rather than in one tool module stops the later stories from each inventing a
slightly different provenance block.
"""

from __future__ import annotations

from datetime import date, datetime

from app.config import get_settings
from pydantic import BaseModel, Field

from mcp_server.errors import InvalidArgument
from mcp_server.resolve import ResolvedPlan


class PlanRef(BaseModel):
    """Which plan answered, and how to ask for it again."""

    source: str = Field(description="'snapshot' (from the store) or 'file' (the default plan).")
    canonical_ref: str = Field(
        description=(
            "Pass this back as `ref` to target this exact plan again. 'default' means "
            "omit the reference — that plan is a file, not a stored snapshot."
        )
    )
    content_hash: str
    version: int | None = None
    kind: str | None = Field(default=None, description="proposal, commit, or baseline.")
    created_at: datetime | None = None
    approved_by: str | None = None
    message: str | None = None
    path: str | None = Field(default=None, description="Set only when source is 'file'.")

    @classmethod
    def of(cls, resolved: ResolvedPlan) -> PlanRef:
        return cls(
            source=resolved.source,
            canonical_ref=resolved.canonical_ref,
            content_hash=resolved.content_hash,
            version=resolved.version,
            kind=resolved.kind,
            created_at=resolved.created_at,
            approved_by=resolved.approved_by,
            message=resolved.message,
            path=resolved.path,
        )


class MilestoneSummary(BaseModel):
    id: str
    name: str
    target_date: date | None = None
    projected_date: date | None = None
    slack_working_days: float | None = Field(
        default=None, description="Negative means the projection misses the target."
    )
    scheduled: bool = Field(
        description=(
            "False when no dependency edge reaches this milestone, so it has a target "
            "date but no projection. Absence of a projected date means unlinked, not "
            "on time."
        )
    )

    @classmethod
    def of(cls, payload: dict) -> MilestoneSummary:
        return cls(
            id=payload["id"],
            name=payload["name"],
            target_date=(
                date.fromisoformat(payload["target_date"]) if payload.get("target_date") else None
            ),
            projected_date=(
                date.fromisoformat(payload["projected_date"])
                if payload.get("projected_date")
                else None
            ),
            slack_working_days=payload.get("slack_working_days"),
            scheduled=bool(payload.get("scheduled")),
        )


def start_date_or_default(start: str | None) -> date:
    """Parse a caller's start date, or fall back to the configured project start."""
    raw = start or get_settings().project_start_date
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidArgument(
            f"{raw!r} is not a valid start date. Use YYYY-MM-DD, or omit it to use "
            f"the configured project start ({get_settings().project_start_date})."
        ) from exc
