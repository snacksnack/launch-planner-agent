"""`drift.check` and `drift.explain` — what the detector found, and why.

The only tools in this server that cross the network. Everything else calls
`planner_core` and `app` in process; the drift detector is a separate service
(`tpm-automation-platform`), so these go over HTTP through the bounded client in
`mcp_server.drift`.

**These read; they never scan.** `POST /drift/run` on that service runs a full
cycle — Jira collect, snapshot write, an Anthropic call, and Slack DMs to real
owners. A model calling that three times while exploring a question would send
three rounds of messages to people. These tools use the read-only endpoints added
in RC1-244 instead, and every response carries the run they came from so a
reader can never mistake stored findings for a live scan.

**A finding is addressed by an opaque id we mint.** Upstream, identity is the
triple `(rule_type, upstream, downstream)` — stable across runs, which a row id
would not be. Rather than make a model reassemble that triple from a list it
just read, `drift.check` returns a `finding_id` on every finding and
`drift.explain` takes it back verbatim. Same contract as `canonical_ref` on the
plan tools: hand back something that resolves, and the caller never constructs
an identifier itself.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Any

from mcp.server import MCPServer
from pydantic import BaseModel, Field

from mcp_server import drift as drift_client
from mcp_server.errors import DriftUnavailable, InvalidArgument, legible_errors

BUCKETS = ("red", "yellow", "white")

#: Field separator inside the encoded id. A unit separator cannot appear in a
#: Jira key or a rule name, so encoding is unambiguous without escaping.
_SEP = "\x1f"


def encode_finding_id(rule_type: str, upstream: str | None, downstream: str) -> str:
    """Pack a finding's identity into one opaque, URL-safe token."""
    raw = _SEP.join((rule_type, upstream or "", downstream))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_finding_id(finding_id: str) -> tuple[str, str | None, str]:
    """Unpack a `finding_id`, or raise a legible error.

    A malformed id fails here rather than reaching the service, where it would
    surface as a bare 404 that reads like "the finding is gone" rather than
    "that identifier was never valid".
    """
    padded = finding_id + "=" * (-len(finding_id) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidArgument(
            f"{finding_id!r} is not a valid finding_id. Use the finding_id returned by "
            "drift.check rather than composing one."
        ) from exc
    parts = raw.split(_SEP)
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise InvalidArgument(
            f"{finding_id!r} is not a valid finding_id. Use the finding_id returned by "
            "drift.check rather than composing one."
        )
    rule_type, upstream, downstream = parts
    return rule_type, upstream or None, downstream


class DriftFinding(BaseModel):
    finding_id: str = Field(
        description="Pass this to drift.explain. Stable across runs; do not compose one."
    )
    rule_type: str
    upstream: str | None = Field(default=None, description="The cause ticket, if the rule has one.")
    downstream: str = Field(description="The affected ticket.")
    severity: float
    severity_bucket: str = Field(description="red, yellow, or white.")
    detail: str = Field(
        description=(
            "Why the rule fired, in evidence terms — the dates and the change that "
            "triggered it."
        )
    )
    first_seen_run: int | None = None
    is_new: bool = Field(description="First detected in this run rather than carried over.")

    @classmethod
    def of(cls, payload: dict[str, Any]) -> DriftFinding:
        return cls(
            finding_id=encode_finding_id(
                payload["rule_type"], payload.get("upstream"), payload["downstream"]
            ),
            rule_type=payload["rule_type"],
            upstream=payload.get("upstream"),
            downstream=payload["downstream"],
            severity=payload.get("severity", 0.0),
            severity_bucket=payload.get("severity_bucket", ""),
            detail=payload.get("detail", ""),
            first_seen_run=payload.get("first_seen_run"),
            is_new=bool(payload.get("is_new")),
        )


class DriftReport(BaseModel):
    project_key: str
    run_id: int | None = Field(
        description="The stored run these came from. Null means the detector has never run."
    )
    run_at: datetime | None = Field(
        description="When that run happened. This is not a live scan — quote this alongside."
    )
    is_live: bool = Field(
        default=False,
        description="Always false. These are the last scheduled run's findings, not a fresh scan.",
    )
    counts_by_bucket: dict[str, int]
    count: int
    findings: list[DriftFinding]
    note: str | None = None
    checked_at: datetime


def _report(payload: dict[str, Any], findings: list[DriftFinding], note: str | None) -> DriftReport:
    counts: dict[str, int] = {bucket: 0 for bucket in BUCKETS}
    for finding in findings:
        counts[finding.severity_bucket] = counts.get(finding.severity_bucket, 0) + 1
    run_at = payload.get("run_at")
    return DriftReport(
        project_key=payload.get("project_key", ""),
        run_id=payload.get("run_id"),
        run_at=datetime.fromisoformat(run_at) if run_at else None,
        counts_by_bucket=counts,
        count=len(findings),
        findings=findings,
        note=note,
        checked_at=datetime.now(UTC),
    )


def register(server: MCPServer) -> None:
    @server.tool(
        name="drift.check",
        description=(
            "List what the dependency drift detector currently sees: tickets whose dates "
            "or blockers have moved in a way nobody has reacted to, each with a severity "
            "bucket (red, yellow, white) and the evidence for why the rule fired. Use "
            "this for 'anything at risk', 'what is drifting', or 'what changed that we "
            "have not handled'.\n\n"
            "This reports the LAST SCHEDULED RUN of the detector, not a fresh scan — "
            "`run_at` says when that was, and `is_live` is always false. Say when the "
            "data is from if you report it. Running a fresh scan is a write operation "
            "with real side effects and is deliberately not available here.\n\n"
            "Filter with `bucket` or `rule`, or use `since_run` for findings first seen "
            "at or after a given run. Each finding carries a `finding_id` — pass it to "
            "drift.explain for the full evidence. Read-only, and safe to call repeatedly: "
            "it sends no notifications and triggers no scan."
        ),
    )
    @legible_errors
    def drift_check(
        bucket: str | None = None,
        rule: str | None = None,
        since_run: int | None = None,
    ) -> DriftReport:
        if bucket is not None and bucket not in BUCKETS:
            raise InvalidArgument(
                f"bucket must be one of {', '.join(BUCKETS)} (got {bucket!r})."
            )

        params: dict[str, Any] = {}
        if bucket:
            params["bucket"] = bucket
        if rule:
            params["rule"] = rule
        if since_run is not None:
            params["since_run"] = since_run

        payload = drift_client.get_json("/drift/findings", params or None)
        findings = [DriftFinding.of(item) for item in payload.get("findings", [])]

        note = None
        if payload.get("run_id") is None:
            note = (
                "The drift detector has not completed a run yet, so nothing is known — "
                "this is not the same as 'nothing is drifting'."
            )
        elif not findings:
            filtered = bucket or rule or since_run is not None
            note = (
                "No findings matched those filters in the last run."
                if filtered
                else "The last run found no drift."
            )
        return _report(payload, findings, note)

    @server.tool(
        name="drift.explain",
        description=(
            "Explain one drift finding: which rule fired, the upstream and downstream "
            "tickets, the dates and the change that triggered it, and how severe it is. "
            "Use this after drift.check when someone asks why a finding appeared or "
            "whether it is real.\n\n"
            "Takes the `finding_id` returned by drift.check. Do not compose one — it "
            "encodes the finding's identity and is what keeps the reference valid across "
            "detector runs.\n\n"
            "Like drift.check this reads the last scheduled run, not a live scan. A "
            "finding that has cleared since then returns a not-found error rather than "
            "stale evidence. Read-only: no scan, no notifications."
        ),
    )
    @legible_errors
    def drift_explain(finding_id: str) -> DriftReport:
        rule_type, upstream, downstream = decode_finding_id(finding_id)

        payload = drift_client.get_json(
            f"/drift/findings/{rule_type}/{downstream}",
            {"upstream": upstream} if upstream else None,
        )
        findings = [DriftFinding.of(item) for item in payload.get("findings", [])]
        if not findings:
            # The service 404s for a genuinely unknown finding, so an empty body
            # here means the shape changed rather than the finding clearing.
            raise DriftUnavailable(
                "The drift service returned no finding for that finding_id and did not "
                "report it as missing. Call drift.check to see what is currently open."
            )
        return _report(payload, findings, None)
