"""Backups for the plan of record (RC1-246).

The store enforces immutability hard — triggers reject UPDATE and DELETE at the
storage layer — which protects the audit trail from *tampering* and not at all
from *loss*. The plan of record is one SQLite file on one Fly volume.
Immutability without durability is a guarantee about the wrong axis.

**`VACUUM INTO`, not a file copy.** Since ADR-0028 the store runs in WAL mode, so
committed transactions can still be sitting in the `-wal` sidecar: copying the
`.db` alone silently produces a backup missing the most recent commits — exactly
the ones most worth having. `VACUUM INTO` writes a consistent, fully-checkpointed
copy without blocking writers, so it can run against a live service.

**Destinations are a port.** `LocalDirectory` is the default and what the tests
use; `S3Destination` pushes off the machine, because a backup on the same volume
as the original is not a backup. Same ports-and-adapters split as
`PlanRepository` (ADR-0012) and `JiraTarget` (ADR-0018): the boto3 client is
injected, so the S3 path is testable without credentials and boto3 stays an
optional dependency for anyone not deploying.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: `launch-planner-20260812T134500Z.db` — sorts chronologically as a string,
#: which is what makes "keep the newest N" a lexical operation on every backend.
_KEY_PREFIX = "launch-planner-"
_KEY_SUFFIX = ".db"
_KEY_PATTERN = re.compile(
    rf"^{re.escape(_KEY_PREFIX)}(\d{{8}}T\d{{6}}Z){re.escape(_KEY_SUFFIX)}$"
)
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class BackupError(Exception):
    """A backup or restore could not be completed."""


@dataclass(frozen=True)
class BackupRef:
    key: str
    size_bytes: int
    created_at: datetime | None = None

    @property
    def taken_at(self) -> datetime | None:
        """The timestamp encoded in the key — the moment the data is from.

        Preferred over the destination's own mtime, which reflects when the file
        was *uploaded* and drifts if a backup is re-copied.
        """
        match = _KEY_PATTERN.match(self.key)
        if not match:
            return None
        return datetime.strptime(match.group(1), _STAMP_FORMAT).replace(tzinfo=UTC)


@runtime_checkable
class BackupDestination(Protocol):
    """Where backups live. Implementations must not mutate what they store."""

    def put(self, local: Path, key: str) -> BackupRef: ...

    def list(self) -> list[BackupRef]:
        """Every backup, newest last."""
        ...

    def get(self, key: str, local: Path) -> Path: ...

    def delete(self, key: str) -> None: ...


class LocalDirectory:
    """A directory on disk. The default, and what the tests use.

    On a deployed host this is only a real backup if the directory is on
    different storage from the database — otherwise it dies with the volume.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def put(self, local: Path, key: str) -> BackupRef:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / key
        target.write_bytes(local.read_bytes())
        return BackupRef(key=key, size_bytes=target.stat().st_size)

    def list(self) -> list[BackupRef]:
        if not self.directory.is_dir():
            return []
        found = [
            BackupRef(key=p.name, size_bytes=p.stat().st_size)
            for p in self.directory.iterdir()
            if _KEY_PATTERN.match(p.name)
        ]
        return sorted(found, key=lambda ref: ref.key)

    def get(self, key: str, local: Path) -> Path:
        source = self.directory / key
        if not source.is_file():
            raise BackupError(f"no backup named {key!r} in {self.directory}")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(source.read_bytes())
        return local

    def delete(self, key: str) -> None:
        (self.directory / key).unlink(missing_ok=True)


class S3Destination:
    """An S3-compatible bucket (Tigris on Fly, or anything else).

    The client is injected so this is testable without credentials, and so
    `boto3` stays an optional dependency — nothing in the default install or CI
    needs it.
    """

    def __init__(self, bucket: str, client: Any, prefix: str = "plan-store/") -> None:
        self.bucket = bucket
        self.prefix = prefix
        self._client = client

    def _full(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, local: Path, key: str) -> BackupRef:
        self._client.upload_file(str(local), self.bucket, self._full(key))
        return BackupRef(key=key, size_bytes=local.stat().st_size)

    def list(self) -> list[BackupRef]:
        response = self._client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix)
        found = [
            BackupRef(
                key=item["Key"][len(self.prefix) :],
                size_bytes=item.get("Size", 0),
                created_at=item.get("LastModified"),
            )
            for item in response.get("Contents", [])
            if _KEY_PATTERN.match(item["Key"][len(self.prefix) :])
        ]
        return sorted(found, key=lambda ref: ref.key)

    def get(self, key: str, local: Path) -> Path:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, self._full(key), str(local))
        return local

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._full(key))


def destination_from_settings(settings: Any) -> BackupDestination:
    """The configured destination: S3 when a bucket is set, otherwise a directory.

    `boto3` is imported here rather than at module scope so it stays an optional
    dependency — nothing in the default install, the tests, or CI needs it.
    """
    if not settings.backup_s3_bucket:
        return LocalDirectory(settings.backup_dir)

    try:
        import boto3  # noqa: PLC0415 — optional, only for deployments using S3
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise BackupError(
            "LPA_BACKUP_S3_BUCKET is set but boto3 is not installed. Install the "
            "s3 extra, or unset the bucket to back up to a local directory."
        ) from exc

    client = boto3.client("s3", endpoint_url=settings.backup_s3_endpoint_url or None)
    return S3Destination(
        bucket=settings.backup_s3_bucket, client=client, prefix=settings.backup_s3_prefix
    )


def backup_key(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(_STAMP_FORMAT)
    return f"{_KEY_PREFIX}{stamp}{_KEY_SUFFIX}"


def write_consistent_copy(sqlite_path: str, target: Path) -> Path:
    """`VACUUM INTO` the live database. Safe while the service is running.

    Not a file copy: in WAL mode the newest commits may still be in the `-wal`
    sidecar, so copying the `.db` produces a backup missing exactly the
    transactions most worth keeping.
    """
    if target.exists():
        raise BackupError(f"refusing to overwrite an existing backup at {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        raise BackupError(f"could not back up {sqlite_path}: {exc}") from exc
    finally:
        conn.close()
    return target


def verify(path: Path) -> int:
    """Open a backup as a store and count its snapshots.

    A file that exists is not a backup; one that opens and reads is. Returns the
    snapshot count so a caller can report what was captured.
    """
    from app.store import SQLiteEventStore

    try:
        store = SQLiteEventStore(str(path))
    except sqlite3.Error as exc:
        raise BackupError(f"{path} is not a readable plan store: {exc}") from exc
    try:
        return len(store.history())
    finally:
        store.close()


def prune(destination: BackupDestination, keep: int) -> list[str]:
    """Delete all but the newest `keep` backups. Returns the keys removed."""
    if keep < 1:
        raise BackupError("keep must be at least 1 — pruning every backup is not a policy")
    existing = destination.list()
    doomed = existing[: max(0, len(existing) - keep)]
    for ref in doomed:
        destination.delete(ref.key)
    return [ref.key for ref in doomed]


def run_backup(
    destination: BackupDestination,
    sqlite_path: str,
    *,
    workdir: Path,
    keep: int,
    now: datetime | None = None,
) -> tuple[BackupRef, int, list[str]]:
    """Take a backup, verify it, store it, prune old ones.

    Returns the stored reference, the snapshot count it captured, and the keys
    pruned. Verification happens *before* the upload: shipping a corrupt file
    off-box and pruning a good one behind it would be worse than not running.
    """
    key = backup_key(now)
    staged = write_consistent_copy(sqlite_path, workdir / key)
    snapshots = verify(staged)
    ref = destination.put(staged, key)
    pruned = prune(destination, keep)
    return ref, snapshots, pruned


def restore(destination: BackupDestination, key: str, target: Path) -> int:
    """Fetch a backup and verify it opens. Returns its snapshot count.

    Deliberately restores to a path the caller names rather than over the live
    database. Swapping it in is a deploy step a human does with the service
    stopped — see the HOWTO.
    """
    if target.exists():
        raise BackupError(f"refusing to overwrite {target}; restore to a new path")
    destination.get(key, target)
    return verify(target)
