"""Shared fixtures.

Both settings objects are `lru_cache`d, so a test that changes the environment
must clear both or it reads whatever an earlier test (or the developer's own
`.env`) left behind. `isolated_settings` is autouse for exactly that reason —
without it these tests would read the real `launch_planner.db` from the repo
root and pass or fail depending on the machine.
"""

from __future__ import annotations

import pytest
from app.config import get_settings
from mcp_server.config import get_mcp_settings


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_mcp_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point both settings objects at a temp store and no drift service."""
    monkeypatch.setenv("LPA_DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    # Set to empty rather than deleted: both settings classes also read `.env`,
    # and a real env var overrides it while a missing one does not. Empty is
    # falsy, so `drift_configured` is False either way.
    monkeypatch.setenv("LPA_DRIFT_BASE_URL", "")
    monkeypatch.setenv("LPA_DRIFT_RUN_TOKEN", "")
    _clear_caches()
    yield
    _clear_caches()
