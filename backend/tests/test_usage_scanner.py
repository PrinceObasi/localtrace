import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from localtrace_backend.collectors.usage import DiskUsageScanner
from localtrace_backend.models import CapabilityStatus, UsageTargetStatus


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class Monotonic:
    def __init__(self, step: float = 0.0) -> None:
        self.value = 100.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def names(uid: int) -> str | None:
    return {1000: "alice", 1001: "bob"}.get(uid)


def make_scanner(directories, **kwargs) -> DiskUsageScanner:
    return DiskUsageScanner(
        directories_provider=lambda: directories,
        now=Clock(),
        monotonic=kwargs.pop("monotonic", Monotonic()),
        owner_name=names,
        **kwargs,
    )


def test_bytes_are_grouped_by_owner_with_top_files(tmp_path: Path, monkeypatch) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "big.gguf").write_bytes(b"x" * 1000)
    (models / "nested").mkdir()
    (models / "nested" / "small.bin").write_bytes(b"y" * 100)
    (models / "other.safetensors").write_bytes(b"z" * 300)

    real_lstat = os.lstat

    def fake_owner_stat(entry):
        result = real_lstat(entry.path)
        uid = 1001 if entry.path.endswith("other.safetensors") else 1000
        return os.stat_result(
            (
                result.st_mode,
                result.st_ino,
                result.st_dev,
                result.st_nlink,
                uid,
                result.st_gid,
                result.st_size,
                result.st_atime,
                result.st_mtime,
                result.st_ctime,
            )
        )

    monkeypatch.setattr(
        "localtrace_backend.collectors.usage._entry_stat", fake_owner_stat
    )

    scanner = make_scanner([str(models)], top_files=2)
    result = scanner.scan_now()

    assert result.status == CapabilityStatus.AVAILABLE
    assert result.truncated is False
    assert result.file_count == 3
    assert result.total_apparent_bytes == 1400
    assert [owner.uid for owner in result.owners] == [1000, 1001]
    alice, bob = result.owners
    assert alice.owner_name == "alice"
    assert alice.file_count == 2
    assert alice.apparent_bytes == 1100
    assert alice.share_percent == 78.57
    assert [item.path for item in alice.top_files] == [
        str(models / "big.gguf"),
        str(models / "nested" / "small.bin"),
    ]
    assert bob.apparent_bytes == 300
    assert result.directories[0].status == UsageTargetStatus.SCANNED
    assert "not writer identity" in (result.message or "")


def test_symlinks_and_hard_links_are_not_double_counted(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    blobs = hub / "blobs"
    snapshots = hub / "snapshots" / "abc"
    blobs.mkdir(parents=True)
    snapshots.mkdir(parents=True)
    blob = blobs / "sha256-1"
    blob.write_bytes(b"m" * 500)
    (snapshots / "model.safetensors").symlink_to(blob)
    hard = hub / "hardlink.bin"
    os.link(blob, hard)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.bin").write_bytes(b"q" * 50)
    (hub / "escape").symlink_to(outside, target_is_directory=True)

    result = make_scanner([str(hub)]).scan_now()

    assert result.file_count == 1
    assert result.total_apparent_bytes == 500
    assert result.status == CapabilityStatus.AVAILABLE


def test_file_budget_marks_scan_partial_and_truncated(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for index in range(5):
        (first / f"{index}.bin").write_bytes(b"a" * 10)
    (second / "later.bin").write_bytes(b"b" * 10)

    result = make_scanner([str(first), str(second)], max_files=3).scan_now()

    assert result.status == CapabilityStatus.PARTIAL
    assert result.truncated is True
    assert result.file_count == 3
    assert result.directories[0].status == UsageTargetStatus.PARTIAL
    assert result.directories[1].status == UsageTargetStatus.PARTIAL
    assert "Not scanned" in (result.directories[1].message or "")
    assert "file budget" in (result.message or "")


def test_time_budget_is_enforced(tmp_path: Path) -> None:
    folder = tmp_path / "slow"
    folder.mkdir()
    for index in range(4):
        (folder / f"{index}.bin").write_bytes(b"a")

    result = make_scanner(
        [str(folder)], max_seconds=1.0, monotonic=Monotonic(step=0.6)
    ).scan_now()

    assert result.truncated is True
    assert "time budget" in (result.message or "")
    assert result.file_count < 4


def test_no_watched_directories_is_unavailable_not_zero(tmp_path: Path) -> None:
    result = make_scanner([]).scan_now()

    assert result.status == CapabilityStatus.UNAVAILABLE
    assert result.owners == []
    assert result.directories == []


def test_missing_directory_is_error_for_that_target_only(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    (present / "a.bin").write_bytes(b"1234")
    missing = tmp_path / "missing"

    result = make_scanner([str(present), str(missing)]).scan_now()

    assert result.status == CapabilityStatus.PARTIAL
    assert result.directories[0].status == UsageTargetStatus.SCANNED
    assert result.directories[1].status == UsageTargetStatus.ERROR
    assert result.total_apparent_bytes == 4


def test_all_directories_failing_is_error(tmp_path: Path) -> None:
    result = make_scanner([str(tmp_path / "nope")]).scan_now()

    assert result.status == CapabilityStatus.ERROR


def test_snapshot_before_first_scan_is_warming_up(tmp_path: Path) -> None:
    scanner = make_scanner([str(tmp_path)])

    snapshot = scanner.snapshot()

    assert snapshot.status == CapabilityStatus.WARMING_UP
    assert snapshot.scan_started_at is None
    assert snapshot.owners == []


def test_background_loop_publishes_a_result_and_stops(tmp_path: Path) -> None:
    (tmp_path / "f.bin").write_bytes(b"12")
    scanner = DiskUsageScanner(
        directories_provider=lambda: [str(tmp_path)],
        scan_interval_seconds=0.05,
        owner_name=names,
    )

    scanner.start()
    try:
        deadline = 200
        while scanner.snapshot().status == CapabilityStatus.WARMING_UP and deadline:
            import time

            time.sleep(0.01)
            deadline -= 1
    finally:
        scanner.stop()

    assert scanner.snapshot().status == CapabilityStatus.AVAILABLE
    assert scanner.snapshot().file_count == 1


def test_more_owners_than_the_table_is_partial_with_true_count(tmp_path: Path, monkeypatch) -> None:
    from localtrace_backend.collectors import usage as usage_module

    folder = tmp_path / "many"
    folder.mkdir()
    for index in range(5):
        (folder / f"{index}.bin").write_bytes(b"x" * (index + 1))
    real_lstat = os.lstat

    def per_file_uid(entry):
        result = real_lstat(entry.path)
        uid = 5000 + int(entry.name.split(".")[0])
        return os.stat_result(
            (result.st_mode, result.st_ino, result.st_dev, result.st_nlink, uid,
             result.st_gid, result.st_size, result.st_atime, result.st_mtime, result.st_ctime)
        )

    monkeypatch.setattr("localtrace_backend.collectors.usage._entry_stat", per_file_uid)
    monkeypatch.setattr(usage_module, "MAX_OWNERS", 3)

    result = make_scanner([str(folder)]).scan_now()

    assert result.status == CapabilityStatus.PARTIAL
    assert result.owner_count == 5
    assert len(result.owners) == 3
    assert [owner.uid for owner in result.owners] == [5004, 5003, 5002]
    assert "top 3 of 5 owners" in (result.message or "")
