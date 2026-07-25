"""FastAPI application factory: health, config, and the Gantt data endpoint."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from planner_core import DecisionRecord, Plan, Snapshot, build_decision_record, schedule_plan

from app import __version__
from app.config import get_settings
from app.gantt import build_gantt_payload
from app.store import SQLiteEventStore

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


def _resolve_prd_text(plan: Plan, plan_path: Path | None) -> str | None:
    """Best-effort PRD lookup so the decision record's source-dependent checks
    (unverifiable quotes, coverage gaps) can be recomputed: the plan's
    `source_document`, then a `prd.md` beside the plan or one level up (the golden
    lives in a `golden/` subdir). Returns None if the PRD can't be located."""
    if plan.source_document:
        resolved = _resolve_plan(plan.source_document)
        if resolved is not None:
            return resolved.read_text()
    if plan_path is not None:
        for sibling in (plan_path.parent / "prd.md", plan_path.parent.parent / "prd.md"):
            if sibling.is_file():
                return sibling.read_text()
    return None


def _decisions_for(
    plan: Plan, plan_path: Path | None, persisted: DecisionRecord | None
) -> DecisionRecord:
    """The persisted build-time audit for a committed snapshot, or a fresh one
    recomputed from plan + PRD (the recomputable flags/coverage — a plan rebuilt
    from JSON has no captured rejections/cycle-breaks)."""
    if persisted is not None:
        return persisted
    return build_decision_record(plan, _resolve_prd_text(plan, plan_path))


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
        snapshot: str | None = Query(
            default=None, description="Render a committed snapshot (version or content hash)."
        ),
    ) -> dict[str, object]:
        """Schedule a plan and return the Gantt-ready payload.

        Renders a committed snapshot from the store when `snapshot` is given;
        otherwise a plan file (defaulting to the flagship golden), so the UI
        renders end-to-end without any LLM credentials.
        """
        plan_path: Path | None = None
        persisted_record: DecisionRecord | None = None
        if snapshot is not None:
            snap = _load_snapshot(settings.sqlite_path, snapshot)
            parsed = snap.plan
            persisted_record = snap.decision_record
        else:
            requested = plan or settings.plan_path
            plan_path = _resolve_plan(requested)
            if plan_path is None:
                raise HTTPException(status_code=404, detail=f"plan not found: {requested}")
            parsed = Plan.model_validate_json(plan_path.read_text())

        try:
            start_date = date.fromisoformat(start or settings.project_start_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid start date: {exc}") from exc

        schedule = schedule_plan(parsed, start_date=start_date)
        payload = build_gantt_payload(parsed, schedule)
        payload["decisions"] = _decisions_for(parsed, plan_path, persisted_record).model_dump()
        return payload

    @app.get("/api/history", tags=["plan"])
    def api_history() -> list[dict[str, object]]:
        """The plan-of-record history — committed and proposed snapshots."""
        store = SQLiteEventStore(settings.sqlite_path)
        try:
            return [
                {
                    "version": s.version,
                    "kind": s.kind.value,
                    "content_hash": s.content_hash,
                    "approved_by": s.approved_by,
                    "message": s.message,
                    "created_at": s.created_at.isoformat(),
                }
                for s in store.history()
            ]
        finally:
            store.close()

    return app


def _load_snapshot(sqlite_path: str, ref: str) -> Snapshot:
    store = SQLiteEventStore(sqlite_path)
    try:
        snap = store.get_by_version(int(ref)) if ref.isdigit() else store.get_by_hash(ref)
    finally:
        store.close()
    if snap is None:
        raise HTTPException(status_code=404, detail=f"snapshot not found: {ref}")
    return snap


app = create_app()
