"""FastAPI application factory: health, config, and the Gantt data endpoint."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from planner_core import Plan, schedule_plan

from app import __version__
from app.config import get_settings
from app.gantt import build_gantt_payload

# Repo root, so a relative plan_path resolves no matter where uvicorn is launched
# from (repo root, apps/api, ...). apps/api/app/main.py -> parents[3] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve_plan(path_str: str) -> Path | None:
    """Find a plan file: absolute, or relative to the CWD, or relative to the repo root."""
    candidate = Path(path_str)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for base in (Path.cwd(), _REPO_ROOT):
        resolved = base / candidate
        if resolved.is_file():
            return resolved
    return None


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        summary="Agentic migration/launch planner — LLM proposes, Python validates, human approves.",  # noqa: E501
    )

    # The Gantt UI (Vite dev server) is a separate origin during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # local demo tool; tighten before any real deployment
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "app": settings.app_name,
            "version": __version__,
            "environment": settings.environment,
        }

    @app.get("/api/plan", tags=["plan"])
    def api_plan(
        start: str | None = Query(default=None, description="Project start date (YYYY-MM-DD)."),
        plan: str | None = Query(default=None, description="Path to a plan.json to render."),
    ) -> dict[str, object]:
        """Schedule a plan and return the Gantt-ready payload.

        Defaults to the flagship golden plan and a fixed start date so the UI
        renders end-to-end without any LLM credentials.
        """
        requested = plan or settings.plan_path
        path = _resolve_plan(requested)
        if path is None:
            raise HTTPException(status_code=404, detail=f"plan not found: {requested}")

        parsed = Plan.model_validate_json(path.read_text())
        try:
            start_date = date.fromisoformat(start or settings.project_start_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid start date: {exc}") from exc

        schedule = schedule_plan(parsed, start_date=start_date)
        return build_gantt_payload(parsed, schedule)

    return app


app = create_app()
