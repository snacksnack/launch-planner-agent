"""Shared fixtures.

`EvalSettings` is `lru_cache`d and reads `.env`, exactly like the two settings
objects `apps/mcp/tests/conftest.py` guards — so the same trap applies: without
isolation these tests would append to whatever run log the developer's `.env`
points at, and a suite that writes to a real artifact is a suite nobody trusts
to run twice.

The subject under test manipulates `LPA_DATABASE_URL` itself (that is its job —
see `evals.subjects.health._isolated_world`), so this fixture does not, and
deliberately asserts nothing about it.
"""

from __future__ import annotations

import pytest
from evals.config import get_eval_settings


@pytest.fixture(autouse=True)
def isolated_run_log(tmp_path, monkeypatch):
    """Point the run log at a temp file for the duration of one test."""
    monkeypatch.setenv("LPA_EVALS_RUNS_PATH", str(tmp_path / "runs.jsonl"))
    get_eval_settings.cache_clear()
    yield
    get_eval_settings.cache_clear()
