"""Plan-reference resolution — solved once, here, for every plan tool.

The planner addresses a plan three ways: a version integer, a content hash, or a
path to a `plan.json` (defaulting to the flagship golden). A model will not
reliably pick the right one, and a 64-character sha256 is not something anyone
types into a conversation. So the tools accept a *friendly* reference and this
module turns it into exactly one plan:

    None / omitted   the configured default plan file — works with no store
    "latest"         the newest commit or baseline in the store
    "baseline"       the newest baseline
    12               that snapshot version
    "a3f9"           a content-hash prefix, if it matches exactly one snapshot

Every resolution reports back the canonical version *and* full hash, so a model
can echo an unambiguous reference into its next call or into its answer.

**Paths are deliberately not accepted from a caller.** The HTTP API takes a
`plan=` file path; exposing that to a model would let a conversation read
arbitrary JSON off the host for no benefit — the useful references are the ones
above. The default file stays configurable through `LPA_PLAN_PATH`, which is an
operator's decision rather than a model's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.config import get_settings
from app.store import SQLiteEventStore
from planner_core import InMemoryPlanRepository, Plan, Snapshot, content_hash

from mcp_server.errors import AmbiguousPlanRef, PlanNotFound

_REPO_ROOT = Path(__file__).resolve().parents[3]


def plan_store_exists() -> bool:
    """Whether there is a database to read, without creating one.

    `SQLiteEventStore` runs its migration on construction, so simply opening a
    missing path creates an empty database — a write, from a server whose entire
    claim is that it does not write. Every read path here checks first and
    treats "no file" as "no snapshots", which is what it means.
    """
    sqlite_path = get_settings().sqlite_path
    return sqlite_path == ":memory:" or Path(sqlite_path).exists()

#: A hash prefix must be at least this long. Shorter is almost always a typo,
#: and a 1–3 character prefix matches so much that the error is unhelpful.
MIN_HASH_PREFIX = 4


@dataclass(frozen=True)
class ResolvedPlan:
    """A plan plus the provenance every response has to carry."""

    plan: Plan
    source: Literal["snapshot", "file"]
    content_hash: str
    ref_requested: str
    version: int | None = None
    kind: str | None = None
    created_at: datetime | None = None
    approved_by: str | None = None
    message: str | None = None
    path: str | None = None

    @property
    def canonical_ref(self) -> str:
        """What a caller should pass back to get exactly this plan again.

        Not simply the content hash: a plan read from a *file* is not in the
        store, so its hash resolves to nothing. Echoing one back would hand the
        model a reference that fails on use — the precise failure this whole
        module exists to prevent. The default file's usable reference is "no
        reference at all".
        """
        if self.version is not None:
            return str(self.version)
        if self.source == "file":
            return "default"
        return self.content_hash


def _resolve_plan_file(path_str: str) -> Path | None:
    """Absolute, or relative to the CWD, or relative to the repo root.

    Mirrors `app.main._resolve_plan` so the MCP server and the API disagree
    about nothing when both are pointed at the same `LPA_PLAN_PATH`.
    """
    candidate = Path(path_str)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for base in (Path.cwd(), _REPO_ROOT):
        resolved = base / candidate
        if resolved.is_file():
            return resolved
    return None


def _from_snapshot(snapshot: Snapshot, ref: str) -> ResolvedPlan:
    return ResolvedPlan(
        plan=snapshot.plan,
        source="snapshot",
        content_hash=snapshot.content_hash,
        ref_requested=ref,
        version=snapshot.version,
        kind=snapshot.kind.value,
        created_at=snapshot.created_at,
        approved_by=snapshot.approved_by,
        message=snapshot.message,
    )


def _default_plan_file(ref: str) -> ResolvedPlan:
    settings = get_settings()
    path = _resolve_plan_file(settings.plan_path)
    if path is None:
        raise PlanNotFound(
            f"The configured default plan file ({settings.plan_path}) does not exist. "
            "Call plan.list to see committed snapshots, and pass one by version."
        )
    plan = Plan.model_validate_json(path.read_text())
    return ResolvedPlan(
        plan=plan,
        source="file",
        content_hash=content_hash(plan),
        ref_requested=ref,
        path=str(path),
    )


def _no_snapshots_message(what: str) -> str:
    return (
        f"No {what} exists in the plan store yet — nothing has been committed. "
        "Omit the reference to use the configured default plan, or call plan.list "
        "to see what is available."
    )


def resolve_plan_ref(ref: str | None = None) -> ResolvedPlan:
    """Turn a friendly reference into exactly one plan, or raise legibly."""
    requested = (ref or "").strip()
    if not requested:
        return _default_plan_file("default")

    lowered = requested.lower()
    # Round-trips `canonical_ref` for a file-sourced plan: what we hand back has
    # to be something we accept, or the reference is decorative.
    if lowered == "default":
        return _default_plan_file(requested)

    # With no database yet, stand in the port's own empty reference repository
    # rather than special-casing: every branch below then produces exactly the
    # message it would have given for an empty store, and nothing is created.
    store = (
        SQLiteEventStore(get_settings().sqlite_path)
        if plan_store_exists()
        else InMemoryPlanRepository()
    )
    try:
        if lowered == "latest":
            snapshot = store.latest_of_record()
            if snapshot is None:
                raise PlanNotFound(_no_snapshots_message("committed plan"))
            return _from_snapshot(snapshot, requested)

        if lowered == "baseline":
            snapshot = store.latest_baseline()
            if snapshot is None:
                raise PlanNotFound(_no_snapshots_message("baseline"))
            return _from_snapshot(snapshot, requested)

        if requested.isdigit():
            snapshot = store.get_by_version(int(requested))
            if snapshot is None:
                available = [str(s.version) for s in store.history()]
                listed = ", ".join(available) if available else "none"
                raise PlanNotFound(
                    f"No snapshot at version {requested}. Available versions: {listed}."
                )
            return _from_snapshot(snapshot, requested)

        # Anything else is treated as a content-hash prefix.
        if len(lowered) < MIN_HASH_PREFIX:
            raise PlanNotFound(
                f"{requested!r} is too short to be a content hash (need at least "
                f"{MIN_HASH_PREFIX} characters). Use a version number, 'latest', "
                "'baseline', or a longer hash prefix."
            )
        matches = [s for s in store.history() if s.content_hash.startswith(lowered)]
        if not matches:
            raise PlanNotFound(
                f"No snapshot's content hash starts with {requested!r}. "
                "Call plan.list to see the available versions and hashes."
            )
        if len(matches) > 1:
            raise AmbiguousPlanRef(
                requested, [f"v{s.version} ({s.content_hash[:12]})" for s in matches]
            )
        return _from_snapshot(matches[0], requested)
    finally:
        if isinstance(store, SQLiteEventStore):
            store.close()


def snapshot_history() -> list[Snapshot]:
    """The full snapshot log, oldest first. Read-only, and creates nothing."""
    if not plan_store_exists():
        return []
    store = SQLiteEventStore(get_settings().sqlite_path)
    try:
        return store.history()
    finally:
        store.close()
