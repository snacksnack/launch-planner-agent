"""Eval harness configuration, sourced from the environment (pydantic-settings).

Reuses the ``LPA_`` prefix and the ``.env`` file that ``app.config`` already
reads, for the same reason ``mcp_server.config`` does: one file configures the
API, the CLI, the MCP server, and this.

Only the run log lives here. Nothing a *subject* needs to run is duplicated —
``app.config`` stays the single source of truth for ``sqlite_path`` and
``plan_path``. A harness that carried its own copy of the subject's settings
would eventually measure a configuration nobody actually runs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LPA_",
        extra="ignore",
    )

    # Where run records accumulate. One JSONL file, append-only, committed as an
    # artifact rather than served — RC1-255 decides how the trend view reads it.
    evals_runs_path: str = "./eval-runs/runs.jsonl"

    @property
    def runs_path(self) -> Path:
        return Path(self.evals_runs_path)


@lru_cache
def get_eval_settings() -> EvalSettings:
    return EvalSettings()
