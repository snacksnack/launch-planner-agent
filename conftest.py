"""Workspace-wide test isolation from the developer's `.env`.

`.env.example` opens with *"Copy to `.env` and fill in as needed."* Following
that instruction used to break the suite: `Settings()` reads `.env` relative to
the working directory, `.env.example` ships `LPA_BACKUP_S3_BUCKET=` as an empty
assignment, and pydantic-settings resolves that to `""` rather than `None` — so
`test_credentials_are_optional_at_boot` failed on any configured machine while
staying green in CI, which has no `.env` (RC1-259).

The one failing assertion was the symptom. The gap was that **tests read the
environment of whoever ran them**, so the same commit could pass or fail
depending on the machine — the class of failure that quietly erodes trust in a
suite. `apps/mcp/tests/conftest.py` already documents this trap for its own two
settings objects; nothing covered `apps/api`, and nothing covered `.env` itself.

So this fixture points every settings class at an `.env` that does not exist.
Real environment variables still take precedence, which is what
`monkeypatch.setenv` relies on — so the many tests that already configure
themselves that way are unaffected. What changes is only that an unset value
now resolves to its declared default instead of to whatever happens to be on
disk.

It lives at the repo root rather than in one app's tests because the property
worth having is workspace-wide: no test anywhere reads a developer's `.env`.
"""

from __future__ import annotations

import pytest
from app.config import Settings, get_settings
from evals.config import EvalSettings, get_eval_settings
from mcp_server.config import McpSettings, get_mcp_settings

#: Every env-backed settings class in the workspace. A new one must be added
#: here, or its tests silently start reading the developer's `.env` again.
_SETTINGS_CLASSES = (Settings, McpSettings, EvalSettings)

#: Their `lru_cache`d accessors. Cleared on the way in *and* out: a cached
#: instance built before the redirect would still hold `.env` values, and one
#: built during a test would leak into the next.
_CACHED_ACCESSORS = (get_settings, get_mcp_settings, get_eval_settings)


@pytest.fixture(autouse=True)
def ignore_developer_dotenv(monkeypatch, tmp_path):
    """Point every settings class at an `.env` that isn't there."""
    absent = str(tmp_path / "this-file-does-not-exist.env")
    for settings_class in _SETTINGS_CLASSES:
        monkeypatch.setitem(settings_class.model_config, "env_file", absent)

    for accessor in _CACHED_ACCESSORS:
        accessor.cache_clear()
    yield
    for accessor in _CACHED_ACCESSORS:
        accessor.cache_clear()
