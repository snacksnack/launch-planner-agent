"""FastAPI application factory: health, config, and the Gantt data endpoint."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from planner_core import (
    DecisionRecord,
    Plan,
    PlanDiff,
    SavedScenario,
    Scenario,
    Snapshot,
    assemble_status,
    build_decision_record,
    build_generation_plan,
    compare_versions,
    content_hash,
    fallback_narrative,
    monte_carlo,
    render_html,
    render_markdown,
    schedule_plan,
    simulate,
)
from pydantic import BaseModel

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


class _SaveScenarioBody(BaseModel):
    """Request body for POST /api/scenarios."""

    name: str
    scenario: Scenario
    note: str | None = None
    created_by: str | None = None


def _scenario_impact(plan: Plan, scenario: Scenario, start_date: date) -> dict[str, object]:
    """The launch-date impact of a scenario over a plan — for list/compare views."""
    delta = simulate(plan, scenario, start_date=start_date).delta
    return {
        "finish_shift_days": delta.finish_shift_days,
        "has_launch_impact": delta.has_launch_impact,
        "headline": delta.headline,
    }


def create_app() -> FastAPI:
    settings = get_settings()

    # The public demo needs a snapshot history to show Baseline/Status/audit — the
    # read-only API can't create one — so seed it once from the flagship golden.
    if settings.public_demo:
        from app.seed_demo import seed_if_empty

        seed_if_empty(settings.sqlite_path)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        summary="Agentic migration/launch planner — LLM proposes, Python validates, human approves.",  # noqa: E501
    )

    # The Gantt UI (Vite dev server) is a separate origin during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # local demo tool; tighten before any real deployment
        allow_methods=["GET", "POST"],  # POST for /api/simulate (RC1-190)
        allow_headers=["*"],
    )

    # Per-IP sliding-window rate limit on the compute/API endpoints (RC1-195).
    # The public demo is read-only, but the CPM/diff endpoints still cost CPU, so
    # cap anonymous traffic. Simple in-memory window — fine for a single instance.
    _hits: dict[str, deque[float]] = defaultdict(deque)

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        limit = settings.rate_limit_per_minute
        # Only throttle the shared public demo; local/dev runs are unlimited.
        if settings.public_demo and limit and request.url.path.startswith("/api/"):
            now = time.monotonic()
            client = request.client.host if request.client else "?"
            window = _hits[client]
            while window and window[0] < now - 60:
                window.popleft()
            if len(window) >= limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "rate limit exceeded — this is a shared demo"},
                )
            window.append(now)
        return await call_next(request)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "app": settings.app_name,
            "version": __version__,
            "environment": settings.environment,
        }

    @app.get("/api/info", tags=["ops"])
    def api_info() -> dict[str, object]:
        """What this instance exposes — the public demo is read-only by design."""
        return {
            "app": settings.app_name,
            "version": __version__,
            "public_demo": settings.public_demo,
            # No LLM, plan-of-record, or Jira writes over HTTP — agents + commits are
            # CLI-only. The one mutable surface is the saved-scenario scratchpad, and
            # even that is disabled in the public demo (see `scenario_writes`).
            "writes_enabled": False,
            "agent_endpoints": False,
            "scenario_writes": not settings.public_demo,
            "rate_limit_per_minute": settings.rate_limit_per_minute,
        }

    def _load_request_plan(
        plan: str | None, snapshot: str | None
    ) -> tuple[Plan, Path | None, DecisionRecord | None]:
        """Resolve the plan a request targets: a committed snapshot, or a file
        (defaulting to the flagship golden — no credentials needed)."""
        if snapshot is not None:
            snap = _load_snapshot(settings.sqlite_path, snapshot)
            return snap.plan, None, snap.decision_record
        requested = plan or settings.plan_path
        plan_path = _resolve_plan(requested)
        if plan_path is None:
            raise HTTPException(status_code=404, detail=f"plan not found: {requested}")
        return Plan.model_validate_json(plan_path.read_text()), plan_path, None

    def _request_start_date(start: str | None) -> date:
        try:
            return date.fromisoformat(start or settings.project_start_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid start date: {exc}") from exc

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
        parsed, plan_path, persisted_record = _load_request_plan(plan, snapshot)
        start_date = _request_start_date(start)
        schedule = schedule_plan(parsed, start_date=start_date)
        payload = build_gantt_payload(parsed, schedule, jira_base_url=settings.jira_base_url)
        payload["decisions"] = _decisions_for(parsed, plan_path, persisted_record).model_dump()
        return payload

    @app.post("/api/simulate", tags=["plan"])
    def api_simulate(
        scenario: Scenario,
        start: str | None = Query(default=None, description="Project start (YYYY-MM-DD)."),
        plan: str | None = Query(default=None, description="Path to a plan.json to simulate."),
        snapshot: str | None = Query(
            default=None, description="Simulate over a committed snapshot (version or hash)."
        ),
    ) -> dict[str, object]:
        """Apply a scenario, re-run CPM, and return baseline + simulated Gantt
        payloads with the structured schedule delta — the what-if for the UI."""
        parsed, _, _ = _load_request_plan(plan, snapshot)
        start_date = _request_start_date(start)
        result = simulate(parsed, scenario, start_date=start_date)
        return {
            "baseline": build_gantt_payload(parsed, result.baseline),
            "simulated": build_gantt_payload(result.simulated_plan, result.simulated),
            "delta": result.delta.model_dump(mode="json"),
            "warnings": result.warnings,
        }

    def _require_scenario_writes() -> None:
        if settings.public_demo:
            raise HTTPException(
                status_code=403, detail="saving scenarios is disabled in the read-only demo"
            )

    @app.get("/api/scenarios", tags=["plan"])
    def api_list_scenarios(
        start: str | None = Query(default=None, description="Project start (YYYY-MM-DD)."),
        plan: str | None = Query(default=None, description="Path to a plan.json."),
        snapshot: str | None = Query(default=None, description="A committed snapshot ref."),
    ) -> list[dict[str, object]]:
        """Saved what-if scenarios for the targeted plan, each with its launch impact
        (recomputed, so the list doubles as a side-by-side comparison)."""
        parsed, _, _ = _load_request_plan(plan, snapshot)
        start_date = _request_start_date(start)
        store = SQLiteEventStore(settings.sqlite_path)
        try:
            saved = store.list_scenarios(content_hash(parsed))
        finally:
            store.close()
        return [
            {
                **s.model_dump(mode="json"),
                "impact": _scenario_impact(parsed, s.scenario, start_date),
            }
            for s in saved
        ]

    @app.post("/api/scenarios", tags=["plan"], status_code=201)
    def api_save_scenario(
        body: _SaveScenarioBody,
        start: str | None = Query(default=None, description="Project start (YYYY-MM-DD)."),
        plan: str | None = Query(default=None, description="Path to a plan.json."),
        snapshot: str | None = Query(default=None, description="A committed snapshot ref."),
    ) -> dict[str, object]:
        """Save a named scenario against the targeted plan (keyed by its content hash).
        Overwrites any existing scenario with the same name. Disabled in the demo."""
        _require_scenario_writes()
        if not body.name.strip():
            raise HTTPException(status_code=422, detail="scenario name is required")
        parsed, _, _ = _load_request_plan(plan, snapshot)
        start_date = _request_start_date(start)
        saved = SavedScenario(
            name=body.name.strip(),
            plan_hash=content_hash(parsed),
            scenario=body.scenario,
            created_by=body.created_by,
            created_at=datetime.now(UTC),
            note=body.note,
        )
        store = SQLiteEventStore(settings.sqlite_path)
        try:
            store.save_scenario(saved)
        finally:
            store.close()
        return {
            **saved.model_dump(mode="json"),
            "impact": _scenario_impact(parsed, saved.scenario, start_date),
        }

    @app.delete("/api/scenarios/{name}", tags=["plan"])
    def api_delete_scenario(
        name: str,
        plan: str | None = Query(default=None, description="Path to a plan.json."),
        snapshot: str | None = Query(default=None, description="A committed snapshot ref."),
    ) -> dict[str, object]:
        """Delete a saved scenario for the targeted plan. Disabled in the demo."""
        _require_scenario_writes()
        parsed, _, _ = _load_request_plan(plan, snapshot)
        store = SQLiteEventStore(settings.sqlite_path)
        try:
            deleted = store.delete_scenario(name, content_hash(parsed))
        finally:
            store.close()
        if not deleted:
            raise HTTPException(status_code=404, detail=f"no saved scenario named {name!r}")
        return {"deleted": True, "name": name}

    @app.get("/api/forecast", tags=["plan"])
    def api_forecast(
        start: str | None = Query(default=None, description="Project start (YYYY-MM-DD)."),
        plan: str | None = Query(default=None, description="Path to a plan.json to forecast."),
        snapshot: str | None = Query(
            default=None, description="Forecast over a committed snapshot (version or hash)."
        ),
        iterations: int = Query(
            default=1000, ge=100, le=5000, description="Number of Monte Carlo runs."
        ),
        seed: int = Query(default=0, description="RNG seed — reproducible for a fixed seed."),
    ) -> dict[str, object]:
        """Monte Carlo the launch date over the three-point estimates: a P50/P80/P90
        confidence band, a finish-date distribution, and the per-task criticality
        index. Deterministic for a fixed seed (no randomness reaches the frontend)."""
        parsed, _, _ = _load_request_plan(plan, snapshot)
        start_date = _request_start_date(start)
        result = monte_carlo(parsed, start_date=start_date, iterations=iterations, seed=seed)
        return result.model_dump(mode="json")

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

    @app.get("/api/jira", tags=["plan"])
    def api_jira(
        start: str | None = Query(default=None, description="Project start (YYYY-MM-DD)."),
        plan: str | None = Query(default=None, description="Path to a plan.json."),
        snapshot: str | None = Query(default=None, description="A committed snapshot ref."),
        project: str | None = Query(default=None, description="Jira project key override."),
    ) -> dict[str, object]:
        """The Jira generation plan — the mock preview of exactly what real mode
        would create (RC1-193). Read-only: this endpoint never writes to Jira;
        real writes go through the gated `plan jira --real --confirm` CLI."""
        parsed, _, _ = _load_request_plan(plan, snapshot)
        start_date = _request_start_date(start)
        schedule = schedule_plan(parsed, start_date=start_date)
        project_key = project or settings.jira_project_key
        gen = build_generation_plan(parsed, schedule, project_key=project_key)
        return {
            "generation": gen.model_dump(mode="json"),
            "creates": gen.creates,
            "updates": gen.updates,
            "links": len(gen.links),
            "has_credentials": settings.has_jira_credentials,
        }

    @app.get("/api/baseline", tags=["plan"])
    def api_baseline(
        start: str | None = Query(default=None, description="Project start (YYYY-MM-DD)."),
        current: str | None = Query(
            default=None, description="Current plan ref (version/hash); default the plan file."
        ),
        baseline: str | None = Query(
            default=None, description="Baseline ref (version/hash); default the latest baseline."
        ),
        plan: str | None = Query(default=None, description="Plan file for the current version."),
    ) -> dict[str, object]:
        """Compare a current plan against a baseline: baseline + current Gantt
        payloads and the structural + schedule variance (RC1-192).

        Returns ``{"baseline": null}`` when no baseline has been set, so the UI can
        prompt for one rather than error.
        """
        store = SQLiteEventStore(settings.sqlite_path)
        try:
            base_snap = (
                store.get_by_version(int(baseline))
                if baseline and baseline.isdigit()
                else store.get_by_hash(baseline)
                if baseline
                else store.latest_baseline()
            )
            if base_snap is None:
                return {"baseline": None}
            if current is not None:
                cur_plan = _load_snapshot(settings.sqlite_path, current).plan
            else:
                cur_plan, _, _ = _load_request_plan(plan, None)
        finally:
            store.close()

        start_date = _request_start_date(start)
        comparison = compare_versions(base_snap.plan, cur_plan, start_date=start_date)
        return {
            "baseline": {
                "version": base_snap.version,
                "note": base_snap.message,
                "approved_by": base_snap.approved_by,
                "created_at": base_snap.created_at.isoformat(),
                "payload": build_gantt_payload(
                    base_snap.plan, schedule_plan(base_snap.plan, start_date=start_date)
                ),
            },
            "current": {
                "payload": build_gantt_payload(
                    cur_plan, schedule_plan(cur_plan, start_date=start_date)
                ),
            },
            "comparison": {
                "is_on_track": comparison.is_on_track,
                "plan_diff": _plan_diff_payload(comparison.plan_diff),
                "schedule_delta": comparison.schedule_delta.model_dump(mode="json"),
            },
        }

    @app.get("/api/audit", tags=["plan"])
    def api_audit(
        start: str | None = Query(default=None, description="Project start (YYYY-MM-DD)."),
        plan: str | None = Query(default=None, description="Path to a plan.json."),
        snapshot: str | None = Query(default=None, description="A committed snapshot ref."),
    ) -> dict[str, object]:
        """"How this plan was made" — the reasoning chain for the flagship plan:
        which agents produced what, the deterministic validation actions taken on
        top, and the human review/commit history (RC1-195). Read-only."""
        parsed, plan_path, persisted = _load_request_plan(plan, snapshot)

        # 1. The agent runs, reconstructed from per-entity provenance.
        from collections import defaultdict

        groups: dict[str, dict] = defaultdict(
            lambda: {"model": None, "count": 0, "kinds": defaultdict(int), "timestamp": None}
        )
        buckets = [
            ("epic", parsed.epics),
            ("task", parsed.tasks),
            ("dependency", parsed.dependencies),
            ("milestone", parsed.milestones),
        ]
        for kind, items in buckets:
            for item in items:
                prov = item.provenance
                g = groups[prov.agent]
                g["model"] = prov.model
                g["count"] += 1
                g["kinds"][kind] += 1
                g["timestamp"] = prov.timestamp.isoformat()
        for raid in parsed.raid:
            prov = raid.provenance
            g = groups[prov.agent]
            g["model"] = prov.model
            g["count"] += 1
            g["kinds"]["raid"] += 1
            g["timestamp"] = prov.timestamp.isoformat()
        agents = [
            {
                "agent": name,
                "model": g["model"],
                "count": g["count"],
                "kinds": dict(g["kinds"]),
                "timestamp": g["timestamp"],
            }
            for name, g in sorted(groups.items(), key=lambda kv: kv[1]["timestamp"] or "")
        ]

        # 2. The deterministic validation actions.
        decisions = _decisions_for(parsed, plan_path, persisted).model_dump()

        # 3. The human review / commit history.
        store = SQLiteEventStore(settings.sqlite_path)
        try:
            history = [
                {
                    "version": s.version,
                    "kind": s.kind.value,
                    "content_hash": s.content_hash[:12],
                    "approved_by": s.approved_by,
                    "message": s.message,
                    "created_at": s.created_at.isoformat(),
                }
                for s in store.history()
            ]
        finally:
            store.close()

        return {"agents": agents, "decisions": decisions, "history": history}

    @app.get("/api/status", tags=["plan"])
    def api_status(
        start: str | None = Query(default=None, description="Project start (YYYY-MM-DD)."),
        current: str | None = Query(default=None, description="Current plan ref (version/hash)."),
        baseline: str | None = Query(default=None, description="Baseline ref (default: latest)."),
        period: str | None = Query(default=None, description="Period label."),
        plan: str | None = Query(default=None, description="Plan file for the current version."),
    ) -> dict[str, object]:
        """The weekly status update: deterministic facts + health + a rule-written
        narrative, plus rendered Markdown/HTML (RC1-194). Read-only; never sends.

        The narrative here is the deterministic fallback (credential-free); the LLM
        narrative is produced by the gated `plan status` CLI when a key is set.
        Returns ``{"baseline": null}`` when no baseline exists.
        """
        store = SQLiteEventStore(settings.sqlite_path)
        try:
            base_snap = (
                store.get_by_version(int(baseline))
                if baseline and baseline.isdigit()
                else store.get_by_hash(baseline)
                if baseline
                else store.latest_baseline()
            )
            if base_snap is None:
                return {"baseline": None}
            if current is not None:
                cur_plan = _load_snapshot(settings.sqlite_path, current).plan
            else:
                cur_plan, _, _ = _load_request_plan(plan, None)
        finally:
            store.close()

        start_date = _request_start_date(start)
        comparison = compare_versions(base_snap.plan, cur_plan, start_date=start_date)
        facts = assemble_status(
            comparison,
            baseline_raid=base_snap.plan.raid,
            current_raid=cur_plan.raid,
            period_label=period or "This week",
            baseline_version=base_snap.version,
        )
        narrative = fallback_narrative(facts)
        return {
            "baseline": {"version": base_snap.version, "note": base_snap.message},
            "facts": facts.model_dump(mode="json"),
            "narrative": narrative.model_dump(mode="json"),
            "markdown": render_markdown(facts, narrative),
            "html": render_html(facts, narrative),
        }

    # In production, serve the built web app same-origin so there's one service
    # and no CORS. Mounted last so /api/* and /healthz win. `html=True` serves
    # index.html at "/". Skipped in dev (Vite serves the web itself).
    if settings.web_dist:
        dist = Path(settings.web_dist)
        if dist.is_dir():
            app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")

    return app


def _plan_diff_payload(diff: PlanDiff) -> list[dict[str, object]]:
    """Serialize a structural PlanDiff for the API."""
    return [
        {
            "kind": e.kind,
            "key": e.key,
            "change": e.change,
            "fields": [
                {"field": f.field, "before": f.before, "after": f.after} for f in e.fields
            ],
        }
        for e in diff.entities
    ]


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
