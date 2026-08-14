"""The suite must not read the developer's `.env` (RC1-259).

`.env.example` opens with "Copy to `.env` and fill in as needed." Doing that
turned the suite red on a configured machine while CI — which has no `.env` —
stayed green. The root `conftest.py` fixes it workspace-wide; these tests are
what stop it coming back.
"""

from __future__ import annotations

import pytest
from app.config import Settings


def test_optional_credentials_resolve_to_their_declared_defaults():
    """The exact regression: `.env.example` ships `LPA_BACKUP_S3_BUCKET=`, which
    pydantic-settings resolves to `""` rather than `None`. With a `.env` on disk
    this used to fail; the root conftest points settings at an `.env` that isn't
    there, so an unset value is the declared default again."""
    settings = Settings()

    assert settings.backup_s3_bucket is None
    assert settings.anthropic_api_key is None
    assert settings.jira_base_url is None


def test_environment_variables_still_win():
    """The isolation must not neuter `monkeypatch.setenv` — most of the API
    suite configures itself that way, and a fixture that ignored real env vars
    would break every one of those tests."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LPA_BACKUP_S3_BUCKET", "plans-bucket")
        assert Settings().backup_s3_bucket == "plans-bucket"


@pytest.mark.parametrize(
    "variable,check",
    [
        ("LPA_ANTHROPIC_API_KEY", lambda s: s.has_llm_credentials),
        ("LPA_JIRA_BASE_URL", lambda s: s.has_jira_credentials),
        ("LPA_BACKUP_S3_BUCKET", lambda s: s.backups_go_off_box),
    ],
)
def test_an_empty_optional_credential_never_reads_as_configured(variable, check):
    """An empty assignment in `.env` is how a credential is *not* set, and every
    gate has to agree. These currently pass because each gate uses `bool(...)`
    rather than `is not None` — which is correct, and worth pinning so a later
    `is not None` refactor cannot quietly declare an unconfigured service live.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(variable, "")
        assert check(Settings()) is False
