"""Application configuration, sourced from the environment (pydantic-settings).

All settings are env-backed with the ``LPA_`` prefix and documented in
``.env.example``. Credentials are optional so the app can boot and report a
config sanity check without any secrets present.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LPA_",
        extra="ignore",
    )

    app_name: str = "launch-planner-agent"
    environment: str = "development"

    # Where committed plan-of-record snapshots and baselines live (P1.8+).
    database_url: str = "sqlite:///./launch_planner.db"

    # LLM layer — optional so the service boots without credentials.
    anthropic_api_key: str | None = Field(default=None, repr=False)
    anthropic_model: str = "claude-sonnet-5"

    # Gantt UI (P1.7): which plan to schedule and render, and from when. Defaults
    # to the hand-authored flagship golden so the UI renders with no credentials.
    plan_path: str = "fixtures/jira-cloud-migration/golden/expected-plan.json"
    project_start_date: str = "2026-08-03"  # a Monday

    # Jira export (P3.1) — all optional; real mode is unreachable without them.
    jira_base_url: str | None = None  # e.g. https://your-site.atlassian.net
    jira_email: str | None = Field(default=None, repr=False)
    jira_api_token: str | None = Field(default=None, repr=False)
    jira_project_key: str = "PMA"  # the scratch project real mode writes to

    # Backups (RC1-246). All optional: with none of these set, `plan backup`
    # writes to a local directory, which is fine for development and is NOT a
    # backup in production — it dies with the volume the database is on.
    backup_dir: str = "./backups"
    backup_keep: int = 14  # newest N retained; see ADR-0029 for the cadence
    backup_s3_bucket: str | None = None
    backup_s3_prefix: str = "plan-store/"
    backup_s3_endpoint_url: str | None = None  # Tigris and other S3-compatibles

    # Deploy (P3.3). The public demo is read-only by construction (the API has no
    # LLM/write endpoints); this flag makes that explicit and enables rate limiting.
    public_demo: bool = False
    web_dist: str | None = None  # path to the built web app to serve as static files
    rate_limit_per_minute: int = 60  # per-IP cap on compute endpoints (0 = off)

    @property
    def has_llm_credentials(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_jira_credentials(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_api_token)

    @property
    def backups_go_off_box(self) -> bool:
        """True when backups leave the machine. A local directory does not."""
        return bool(self.backup_s3_bucket)

    @property
    def sqlite_path(self) -> str:
        """The filesystem path for the plan-store SQLite DB, from `database_url`."""
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return self.database_url[len(prefix) :]
        return self.database_url  # already a bare path or ":memory:"

    def sanity_check(self) -> dict[str, object]:
        """A credential-free summary of effective configuration."""
        return {
            "app_name": self.app_name,
            "environment": self.environment,
            "database_url": self.database_url,
            "anthropic_model": self.anthropic_model,
            "anthropic_api_key": "set" if self.has_llm_credentials else "missing",
            "plan_path": self.plan_path,
            "project_start_date": self.project_start_date,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
