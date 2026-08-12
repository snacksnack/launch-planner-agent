"""Backups for the plan of record (RC1-246).

The assertion that matters most is not "a file appeared" — it is that a restored
backup carries the *same provenance*: versions, content hashes, and approvers. A
backup that loses those has lost the thing the audit trail is for.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.backup import (
    BackupError,
    BackupRef,
    LocalDirectory,
    S3Destination,
    backup_key,
    destination_from_settings,
    prune,
    restore,
    run_backup,
    verify,
    write_consistent_copy,
)
from app.config import Settings
from app.store import SQLiteEventStore
from planner_core import Plan, Snapshot, SnapshotKind, content_hash

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "fixtures/jira-cloud-migration/golden/expected-plan.json"
)


def _plan(name: str | None = None) -> Plan:
    plan = Plan.model_validate_json(GOLDEN.read_text())
    return plan.model_copy(update={"name": name}) if name else plan


@pytest.fixture
def live_store(tmp_path):
    """A store with real history, left open — backups must work against a live DB."""
    store = SQLiteEventStore(str(tmp_path / "plans.db"))
    for kind, who, name in (
        (SnapshotKind.PROPOSAL, "agent", "v1"),
        (SnapshotKind.BASELINE, "Priya Nair", "v2"),
        (SnapshotKind.COMMIT, "Marcus Bell", "v3"),
    ):
        plan = _plan(name)
        store.append(
            Snapshot(
                kind=kind,
                plan=plan,
                content_hash=content_hash(plan),
                created_at=datetime.now(UTC),
                approved_by=who,
                message=f"{kind.value} note",
            )
        )
    yield store
    store.close()


@pytest.fixture
def destination(tmp_path):
    return LocalDirectory(tmp_path / "backups")


# --- VACUUM INTO, not a file copy -------------------------------------------


def test_a_backup_can_be_taken_while_the_store_is_open(live_store, tmp_path):
    target = write_consistent_copy(live_store._path, tmp_path / "copy.db")
    assert verify(target) == 3


def test_a_raw_file_copy_would_miss_recent_commits(live_store, tmp_path):
    """Why VACUUM INTO rather than shutil.copy. Under WAL (ADR-0028) the newest
    commits can still be in the -wal sidecar, so copying the .db alone silently
    produces a backup missing exactly the transactions worth keeping."""
    naive = tmp_path / "naive.db"
    naive.write_bytes(Path(live_store._path).read_bytes())

    proper = write_consistent_copy(live_store._path, tmp_path / "proper.db")

    assert verify(proper) == 3
    assert verify(naive) < 3, "the raw copy unexpectedly captured everything"


def test_a_backup_refuses_to_overwrite(live_store, tmp_path):
    target = write_consistent_copy(live_store._path, tmp_path / "copy.db")
    with pytest.raises(BackupError, match="refusing to overwrite"):
        write_consistent_copy(live_store._path, target)


def test_verify_rejects_a_file_that_is_not_a_store(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_text("not a database")
    with pytest.raises((BackupError, sqlite3.DatabaseError)):
        verify(junk)


# --- provenance survives the round trip -------------------------------------


def test_a_restored_backup_carries_identical_provenance(live_store, destination, tmp_path):
    """The load-bearing assertion. A backup that loses versions, hashes, or
    approvers has lost the point of an audit trail."""
    before = [
        (s.version, s.kind, s.content_hash, s.approved_by, s.message)
        for s in live_store.history()
    ]

    ref, snapshots, _ = run_backup(
        destination, live_store._path, workdir=tmp_path / "work", keep=5
    )
    assert snapshots == 3

    target = tmp_path / "restored.db"
    assert restore(destination, ref.key, target) == 3

    restored = SQLiteEventStore(str(target))
    try:
        after = [
            (s.version, s.kind, s.content_hash, s.approved_by, s.message)
            for s in restored.history()
        ]
    finally:
        restored.close()

    assert after == before


def test_the_restored_plans_are_byte_identical(live_store, destination, tmp_path):
    ref, _, _ = run_backup(destination, live_store._path, workdir=tmp_path / "w", keep=5)
    target = tmp_path / "restored.db"
    restore(destination, ref.key, target)

    restored = SQLiteEventStore(str(target))
    try:
        for original, copy in zip(live_store.history(), restored.history(), strict=True):
            assert content_hash(copy.plan) == original.content_hash
    finally:
        restored.close()


def test_restore_refuses_to_overwrite(live_store, destination, tmp_path):
    """Never in place. Swapping a backup in is a deploy step with the service
    stopped, not something a restore command should do behind your back."""
    ref, _, _ = run_backup(destination, live_store._path, workdir=tmp_path / "w", keep=5)
    with pytest.raises(BackupError, match="refusing to overwrite"):
        restore(destination, ref.key, Path(live_store._path))


def test_restoring_an_unknown_key_fails_clearly(destination, tmp_path):
    with pytest.raises(BackupError, match="no backup named"):
        restore(destination, "launch-planner-20260101T000000Z.db", tmp_path / "x.db")


# --- keys and retention -----------------------------------------------------


def test_keys_are_timestamped_and_sort_chronologically():
    early = backup_key(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    late = backup_key(datetime(2026, 11, 2, 3, 4, 5, tzinfo=UTC))
    assert early == "launch-planner-20260102T030405Z.db"
    assert early < late  # lexical order == chronological order, on every backend


def test_a_key_reports_when_the_data_is_from():
    stamp = datetime(2026, 8, 12, 13, 45, 0, tzinfo=UTC)
    assert BackupRef(key=backup_key(stamp), size_bytes=0).taken_at == stamp


def test_pruning_keeps_the_newest(destination, tmp_path):
    source = tmp_path / "seed.db"
    source.write_bytes(b"x")
    keys = [
        backup_key(datetime(2026, 8, day, tzinfo=UTC)) for day in range(1, 6)
    ]
    for key in keys:
        destination.put(source, key)

    pruned = prune(destination, keep=2)

    assert pruned == keys[:3]
    assert [ref.key for ref in destination.list()] == keys[3:]


def test_pruning_never_empties_the_shelf(destination):
    with pytest.raises(BackupError, match="not a policy"):
        prune(destination, keep=0)


def test_a_backup_run_prunes_as_it_goes(live_store, destination, tmp_path):
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for i in range(4):
        run_backup(
            destination,
            live_store._path,
            workdir=tmp_path / f"w{i}",
            keep=2,
            now=base + timedelta(days=i),
        )
    assert len(destination.list()) == 2


def test_unrelated_files_in_the_directory_are_ignored(destination, tmp_path):
    destination.directory.mkdir(parents=True, exist_ok=True)
    (destination.directory / "notes.txt").write_text("hello")
    (destination.directory / "plans.db").write_bytes(b"x")
    assert destination.list() == []


# --- the S3 destination, without credentials --------------------------------


class FakeS3:
    """Just enough boto3 surface to exercise `S3Destination`."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_file(self, local: str, bucket: str, key: str) -> None:
        self.objects[key] = Path(local).read_bytes()

    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict:  # noqa: N803 - boto3 casing
        return {
            "Contents": [
                {"Key": k, "Size": len(v)}
                for k, v in self.objects.items()
                if k.startswith(Prefix)
            ]
        }

    def download_file(self, bucket: str, key: str, local: str) -> None:
        Path(local).write_bytes(self.objects[key])

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803 - boto3 casing
        self.objects.pop(Key, None)


def test_the_s3_destination_round_trips(live_store, tmp_path):
    """The client is injected, so this runs with no credentials and no boto3."""
    fake = FakeS3()
    destination = S3Destination(bucket="plans", client=fake, prefix="plan-store/")

    ref, snapshots, _ = run_backup(
        destination, live_store._path, workdir=tmp_path / "w", keep=5
    )
    assert snapshots == 3
    assert list(fake.objects) == [f"plan-store/{ref.key}"]

    target = tmp_path / "restored.db"
    assert restore(destination, ref.key, target) == 3


def test_the_s3_destination_prunes_by_key(tmp_path):
    fake = FakeS3()
    destination = S3Destination(bucket="plans", client=fake, prefix="p/")
    source = tmp_path / "seed.db"
    source.write_bytes(b"x")
    for day in range(1, 4):
        destination.put(source, backup_key(datetime(2026, 8, day, tzinfo=UTC)))

    assert prune(destination, keep=1) == [
        backup_key(datetime(2026, 8, 1, tzinfo=UTC)),
        backup_key(datetime(2026, 8, 2, tzinfo=UTC)),
    ]
    assert len(fake.objects) == 1


def test_the_prefix_is_stripped_from_listed_keys(tmp_path):
    """A key a caller passes back must be the same key it was given."""
    fake = FakeS3()
    destination = S3Destination(bucket="plans", client=fake, prefix="deep/nested/")
    source = tmp_path / "seed.db"
    source.write_bytes(b"x")
    key = backup_key(datetime(2026, 8, 1, tzinfo=UTC))
    destination.put(source, key)

    assert [ref.key for ref in destination.list()] == [key]


# --- configuration ----------------------------------------------------------


def test_a_local_directory_is_the_default(tmp_path):
    settings = Settings(backup_dir=str(tmp_path), backup_s3_bucket=None)
    assert isinstance(destination_from_settings(settings), LocalDirectory)
    assert settings.backups_go_off_box is False


def test_a_bucket_means_backups_leave_the_machine():
    assert Settings(backup_s3_bucket="plans").backups_go_off_box is True


def test_credentials_are_optional_at_boot():
    """Nothing about backups may stop the app or the tests from starting."""
    settings = Settings()
    assert settings.backup_s3_bucket is None
    assert settings.backup_keep >= 1
