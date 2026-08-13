"""`platform.health` — the walking skeleton.

Deliberately the cheapest subject that exists: deterministic, no model, no
tokens. It is here to prove the harness end to end — case → run → score → record
— before a single golden set is written, the same role `platform.health` itself
played for the MCP server's transport and error mapping.

It is not a thorough health eval and should not grow into one; `apps/mcp/tests`
already asserts this tool's behaviour directly and more cheaply. What the eval
adds is *shape*: characteristics rather than exact values, a case whose input is
world state rather than arguments, and a run record carrying version, cost, and
latency for a subject where the cost is zero. RC1-249 is the first subject where
the measurement itself is the point.

The tool is called in process via `build_server()`, which is the same code path
`apps/mcp/tests` uses. RC1-249 drives a real stdio client session instead —
correct there, because tool *selection* is a property of the surface a client
sees, and pointless here, where the tool takes no arguments and there is nothing
to select.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.store import SQLiteEventStore
from mcp_server import __version__ as mcp_version
from mcp_server.config import get_mcp_settings
from mcp_server.server import build_server
from planner_core import Plan, Snapshot, SnapshotKind, content_hash

from evals.case import Case
from evals.record import CaseResult, CharacteristicResult, SubjectVersion, Usage

NAME = "health"

# apps/evals/evals/subjects/health.py -> apps/evals/evals -> apps/evals -> apps -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_GOLDEN = _REPO_ROOT / "fixtures/jira-cloud-migration/golden/expected-plan.json"

_ALLOWED_STATES = {"ok", "unavailable", "not_configured"}

# Strings that mean an exception repr leaked into a field a model is meant to
# repeat verbatim. `platform.health` promises prose, not a stack trace.
_LEAKED_INTERNALS = ("Traceback", "Error:", "Exception", '  File "')


CASES: tuple[Case, ...] = (
    Case(
        id="health.empty-store",
        input={"snapshots": 0},
        expect=(
            "reports-all-components",
            "states-are-from-the-allowed-set",
            "details-are-model-repeatable",
            "declares-server-version",
            "timestamp-is-fresh",
            "reports-store-absent-without-creating-it",
        ),
        tags=("edge-case", "deterministic"),
    ),
    Case(
        id="health.populated-store",
        input={"snapshots": 1},
        expect=(
            "reports-all-components",
            "states-are-from-the-allowed-set",
            "details-are-model-repeatable",
            "declares-server-version",
            "timestamp-is-fresh",
            "reports-snapshot-count",
        ),
        tags=("deterministic",),
    ),
)


def version() -> SubjectVersion:
    """`model` and `prompt_version` stay None: this subject reaches no model and
    renders no prompt, and saying so explicitly is the point of the field."""
    return SubjectVersion(
        subject=NAME,
        code_version=mcp_version,
        model=None,
        prompt_version=None,
    )


@contextmanager
def _isolated_world(snapshots: int, tmp_root: Path) -> Iterator[None]:
    """Point the planner at a scratch store holding `snapshots` snapshots.

    Both settings objects are `lru_cache`d and both read `.env`, so the caches
    have to be cleared on the way in *and* on the way out — the same trap
    `apps/mcp/tests/conftest.py` documents. Without this the eval would read the
    developer's own `launch_planner.db` and score differently on every machine,
    which is the one thing a regression suite cannot afford.

    `LPA_DRIFT_BASE_URL` is set to empty rather than deleted: a real env var
    overrides `.env` while a missing one does not.
    """
    previous = {
        key: os.environ.get(key)
        for key in ("LPA_DATABASE_URL", "LPA_DRIFT_BASE_URL", "LPA_DRIFT_RUN_TOKEN")
    }
    db_path = tmp_root / "plans.db"
    os.environ["LPA_DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["LPA_DRIFT_BASE_URL"] = ""
    os.environ["LPA_DRIFT_RUN_TOKEN"] = ""
    _clear_settings_caches()
    try:
        if snapshots:
            _seed(snapshots)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _clear_settings_caches()


def _clear_settings_caches() -> None:
    get_settings.cache_clear()
    get_mcp_settings.cache_clear()


def _seed(count: int) -> None:
    """Commit `count` snapshots of the flagship golden so the store has history."""
    plan = Plan.model_validate_json(_GOLDEN.read_text())
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        for index in range(count):
            store.append(
                Snapshot(
                    kind=SnapshotKind.COMMIT,
                    plan=plan,
                    content_hash=content_hash(plan),
                    created_at=datetime.now(UTC),
                    approved_by="evals",
                    message=f"seed {index + 1}",
                )
            )
    finally:
        store.close()


def _call() -> dict:
    server = build_server()
    result = asyncio.run(server.call_tool("platform.health", {}))
    if result.is_error:
        raise RuntimeError(f"platform.health returned an error result: {result}")
    return result.structured_content


class _Context:
    """What a characteristic needs to decide: the case, the output, and the
    window the call happened in."""

    def __init__(
        self,
        case: Case,
        payload: dict,
        before: datetime,
        after: datetime,
        sqlite_path: str,
    ) -> None:
        self.case = case
        self.payload = payload
        self.before = before
        self.after = after
        self.sqlite_path = sqlite_path


# --- characteristics -------------------------------------------------------
#
# Each takes the scoring context and returns (passed, detail). `detail` is
# written to be read in a failure report, so it says what was seen, not just
# that something was wrong.


def _reports_all_components(ctx: _Context) -> tuple[bool, str]:
    missing = [key for key in ("plan_store", "drift_service") if key not in ctx.payload]
    if missing:
        return False, f"missing component(s): {', '.join(missing)}"
    return True, "plan_store and drift_service both reported"


def _states_are_from_the_allowed_set(ctx: _Context) -> tuple[bool, str]:
    seen = {key: ctx.payload.get(key, {}).get("state") for key in ("plan_store", "drift_service")}
    bad = {key: state for key, state in seen.items() if state not in _ALLOWED_STATES}
    if bad:
        return False, f"unrecognised state(s): {bad}"
    return True, f"states {seen}"


def _details_are_model_repeatable(ctx: _Context) -> tuple[bool, str]:
    """A model repeats these verbatim to a human, so an empty string or a leaked
    exception repr is a quality failure even when the state is correct."""
    for key in ("plan_store", "drift_service"):
        detail = ctx.payload.get(key, {}).get("detail", "")
        if not detail.strip():
            return False, f"{key} detail is empty"
        leaked = [marker for marker in _LEAKED_INTERNALS if marker in detail]
        if leaked:
            return False, f"{key} detail leaks internals ({leaked[0]!r}): {detail!r}"
    return True, "both details are non-empty prose"


def _declares_server_version(ctx: _Context) -> tuple[bool, str]:
    version_string = ctx.payload.get("server_version", "")
    if not version_string:
        return False, "server_version is absent or empty"
    return True, f"server_version {version_string!r}"


def _timestamp_is_fresh(ctx: _Context) -> tuple[bool, str]:
    raw = ctx.payload.get("checked_at")
    if not raw:
        return False, "checked_at is absent"
    checked_at = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    if not ctx.before <= checked_at <= ctx.after:
        return False, f"checked_at {checked_at} is outside the call window"
    return True, "checked_at falls inside the call window"


def _reports_store_absent_without_creating_it(ctx: _Context) -> tuple[bool, str]:
    """The store's own acceptance criterion, restated as a characteristic: a
    read-only diagnostic must not bring the database into existence."""
    detail = ctx.payload.get("plan_store", {}).get("detail", "")
    if "no plan store" not in detail:
        return False, f"expected an absent-store detail, got {detail!r}"
    if Path(ctx.sqlite_path).exists():
        return False, f"the health check created {ctx.sqlite_path}"
    return True, "absent store reported, and nothing was created"


def _reports_snapshot_count(ctx: _Context) -> tuple[bool, str]:
    expected = ctx.case.input.get("snapshots", 0)
    detail = ctx.payload.get("plan_store", {}).get("detail", "")
    if f"{expected} snapshot(s)" not in detail:
        return False, f"expected {expected} snapshot(s) in the detail, got {detail!r}"
    return True, f"{expected} snapshot(s) reported"


CHARACTERISTICS: dict[str, Callable[[_Context], tuple[bool, str]]] = {
    "reports-all-components": _reports_all_components,
    "states-are-from-the-allowed-set": _states_are_from_the_allowed_set,
    "details-are-model-repeatable": _details_are_model_repeatable,
    "declares-server-version": _declares_server_version,
    "timestamp-is-fresh": _timestamp_is_fresh,
    "reports-store-absent-without-creating-it": _reports_store_absent_without_creating_it,
    "reports-snapshot-count": _reports_snapshot_count,
}


def run(case: Case, tmp_root: Path) -> CaseResult:
    """Run one case and score it.

    `tmp_root` is passed in rather than created here so the caller controls the
    lifetime — the CLI uses a `TemporaryDirectory`, tests use pytest's
    `tmp_path`, and neither has to trust this function to clean up.
    """
    with _isolated_world(case.input.get("snapshots", 0), tmp_root):
        sqlite_path = get_settings().sqlite_path
        before = datetime.now(UTC)
        started = time.perf_counter()
        try:
            payload = _call()
        except Exception as exc:
            # An outage is a recorded outcome, not a crash: the run continues and
            # `error` marks this case as unscored rather than as a quality drop.
            latency_ms = (time.perf_counter() - started) * 1000
            return CaseResult(
                case_id=case.id,
                usage=Usage(latency_ms=latency_ms),
                error=f"{type(exc).__name__}: {exc}",
            )
        latency_ms = (time.perf_counter() - started) * 1000
        after = datetime.now(UTC)
        ctx = _Context(case, payload, before, after, sqlite_path)

    results = []
    for name in case.expect:
        predicate = CHARACTERISTICS.get(name)
        if predicate is None:
            # An unknown characteristic is a failure, never a skip. Silently
            # ignoring it would let a typo in a case file read as a pass.
            results.append(
                CharacteristicResult(
                    name=name, passed=False, detail="no predicate is registered for this name"
                )
            )
            continue
        passed, detail = predicate(ctx)
        results.append(CharacteristicResult(name=name, passed=passed, detail=detail))

    # Zero tokens and zero cost, recorded rather than omitted — see `Usage`.
    return CaseResult(case_id=case.id, characteristics=results, usage=Usage(latency_ms=latency_ms))
