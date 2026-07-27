"""Tests for RealJiraTarget — the httpx adapter, exercised against a mock transport.

No real Jira is contacted: `httpx.MockTransport` intercepts requests so we can
assert the adapter builds the correct REST bodies (fields, ADF description, due
date, parent, issue link) — the real-mode half of "mock matches real".
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from app.jira_client import RealJiraTarget, _adf


def _target(handler) -> RealJiraTarget:
    t = RealJiraTarget(base_url="https://example.atlassian.net", email="me@x.com", api_token="tok")
    t._client = httpx.Client(
        base_url="https://example.atlassian.net",
        auth=("me@x.com", "tok"),
        transport=httpx.MockTransport(handler),
    )
    return t


def test_adf_wraps_newlines_as_paragraphs():
    doc = _adf("line one\n\nline three")
    assert doc["type"] == "doc" and doc["version"] == 1
    assert [len(p.get("content", [])) for p in doc["content"]] == [1, 0, 1]
    assert doc["content"][0]["content"][0]["text"] == "line one"


def test_create_issue_posts_fields_adf_duedate_and_parent():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(201, json={"key": "PMA-42"})

    target = _target(handler)
    key = target.create_issue(
        project_key="PMA", issue_type="Story", summary="Do the thing",
        description="body text", labels=["launch-planner"], due_date=date(2026, 8, 7),
        parent_key="PMA-1",
    )
    assert key == "PMA-42"
    assert captured["url"].endswith("/rest/api/3/issue")
    fields = captured["json"]["fields"]
    assert fields["project"] == {"key": "PMA"}
    assert fields["issuetype"] == {"name": "Story"}
    assert fields["duedate"] == "2026-08-07"
    assert fields["parent"] == {"key": "PMA-1"}
    assert fields["description"]["type"] == "doc"  # ADF, not raw string


def test_create_link_posts_blocks_with_outward_and_inward():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(201)

    target = _target(handler)
    target.create_link(link_type="Blocks", outward_key="PMA-1", inward_key="PMA-2")
    assert captured["url"].endswith("/rest/api/3/issueLink")
    assert captured["json"] == {
        "type": {"name": "Blocks"},
        "outwardIssue": {"key": "PMA-1"},
        "inwardIssue": {"key": "PMA-2"},
    }


def test_http_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": {"summary": "required"}})

    target = _target(handler)
    with pytest.raises(httpx.HTTPStatusError):
        target.create_issue(
            project_key="PMA", issue_type="Story", summary="", description="d",
            labels=[], due_date=None, parent_key=None,
        )
