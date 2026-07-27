"""RealJiraTarget — the httpx adapter that actually writes to Jira Cloud.

The `app` layer owns network I/O, exactly like the SQLite store sits behind the
`PlanRepository` port. This implements `planner_core.JiraTarget` against the Jira
Cloud REST API v3, so the *same* generation plan that renders the mock preview can
create real issues, links, and due dates — nothing here re-decides what to write.

It is never the default: a run reaches this class only in explicit real mode with
credentials configured. Descriptions are converted to Atlassian Document Format
(ADF), which the v3 API requires.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx


def _adf(text: str) -> dict[str, Any]:
    """Wrap plain text (newline-separated) in a minimal ADF document."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else
        {"type": "paragraph", "content": []}
        for line in text.split("\n")
    ]
    return {"type": "doc", "version": 1, "content": paragraphs or [{"type": "paragraph"}]}


class RealJiraTarget:
    """Creates issues/links in a Jira Cloud project via the REST API (real mode)."""

    def __init__(
        self, *, base_url: str, email: str, api_token: str, timeout: float = 30.0
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(email, api_token),
            timeout=timeout,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def create_issue(
        self,
        *,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
        labels: list[str],
        due_date: date | None,
        parent_key: str | None,
    ) -> str:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": _adf(description),
            "labels": labels,
        }
        if due_date is not None:
            fields["duedate"] = due_date.isoformat()
        if parent_key is not None:
            fields["parent"] = {"key": parent_key}
        resp = self._client.post("/rest/api/3/issue", json={"fields": fields})
        resp.raise_for_status()
        return resp.json()["key"]

    def update_issue(
        self,
        key: str,
        *,
        summary: str,
        description: str,
        labels: list[str],
        due_date: date | None,
    ) -> None:
        fields: dict[str, Any] = {
            "summary": summary,
            "description": _adf(description),
            "labels": labels,
        }
        if due_date is not None:
            fields["duedate"] = due_date.isoformat()
        resp = self._client.put(f"/rest/api/3/issue/{key}", json={"fields": fields})
        resp.raise_for_status()

    def create_link(self, *, link_type: str, outward_key: str, inward_key: str) -> None:
        resp = self._client.post(
            "/rest/api/3/issueLink",
            json={
                "type": {"name": link_type},
                "outwardIssue": {"key": outward_key},
                "inwardIssue": {"key": inward_key},
            },
        )
        resp.raise_for_status()
