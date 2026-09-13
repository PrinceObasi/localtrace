import os
import stat
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from localtrace_backend.collectors.fs_benchmark import FilesystemBenchmarkService
from localtrace_backend.models import (
    BenchmarkRequest,
    BenchmarkStatus,
    CapabilityStatus,
    Volume,
    VolumesResponse,
)

NOW = datetime(2026, 9, 12, 19, 0, tzinfo=timezone.utc)
Usage = namedtuple("Usage", "total used free")


def volume(mount: str, *, family: str = "apfs", remote: bool = False, read_only: bool = False) -> Volume:
    return Volume(
        id=mount.replace("/", "_") or "root",
        name=mount,
        mount_point=mount,
        device="/dev/disk9",
        filesystem=family,
        filesystem_family=family,
        remote=remote,
        read_only=read_only,
        total_bytes=10_000_000_000,
        used_bytes=1_000_000_000,
        available_bytes=9_000_000_000,
        used_percent=10.0,
        health={"status": "unavailable"},
    )


def volumes(*items: Volume) -> VolumesResponse:
    return VolumesResponse(sampled_at=NOW, status=CapabilityStatus.AVAILABLE, source="t", items=list(items))


def make_service(vols: VolumesResponse | None, *, free: int = 10_000_000_000, system: str = "Darwin", cap: int = 64 * 1024 * 1024) -> FilesystemBenchmarkService:
    return FilesystemBenchmarkService(
        volumes_provider=lambda: vols,
        max_size_bytes=cap,
        system_provider=lambda: system,
        disk_usage=lambda _path: Usage(1, 1, free),
        now=lambda: NOW,
    )


def test_run_measures_write_and_read_and_removes_the_file(tmp_path: Path) -> None:
    vols = volumes(volume("/"), volume(str(tmp_path), family="nfs", remote=True))
    service = make_service(vols)

    job = service.start(BenchmarkRequest(directory=str(tmp_path), size_bytes=2 * 1024 * 1024, block_bytes=256 * 1024))
    assert job.status == BenchmarkStatus.RUNNING
    assert job.mount_point == str(tmp_path), "deepest containing mount wins over /"
    assert job.filesystem_family == "nfs"
    assert job.remote is True
    service.wait()

    done = service.get(job.id)
    assert done is not None
    assert done.status == BenchmarkStatus.COMPLETED, done.message
    assert done.write is not None and done.read is not None
    assert done.write.bytes == 2 * 1024 * 1024
    assert done.read.bytes == 2 * 1024 * 1024
    assert done.write.bytes_per_second > 0
    assert done.read.gb_per_second >= 0
    assert done.write.flush_seconds is not None
    assert done.progress_percent == 100
    bench_dir = tmp_path / "localtrace-benchmark"
    assert bench_dir.is_dir()
    assert list(bench_dir.iterdir()) == [], "temporary file must be removed"
    assert stat.S_IMODE(bench_dir.stat().st_mode) == 0o700
    if hasattr(os, "F_NOCACHE"):
        assert done.cache_bypass is True
        assert done.flush_method == "F_FULLFSYNC"
    else:
        assert done.cache_bypass is False
        assert done.flush_method == "fsync"
        assert "buffer cache" in (done.message or "")


def test_root_mount_defaults_to_temp_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    service = make_service(volumes(volume("/")))

    job = service.start(BenchmarkRequest(mount_point="/", size_bytes=1024 * 1024))
    service.wait()

    assert job.directory == str(tmp_path / "localtrace-benchmark")
    assert service.get(job.id).status == BenchmarkStatus.COMPLETED


def test_refusals(tmp_path: Path) -> None:
    service = make_service(volumes(volume("/")))

    def refused(request: BenchmarkRequest, fragment: str) -> None:
        try:
            service.start(request)
        except ValueError as exc:
            assert fragment in str(exc), str(exc)
        else:
            raise AssertionError(f"expected refusal containing {fragment!r}")

    refused(BenchmarkRequest(directory=str(tmp_path), size_bytes=64 * 1024 * 1024 + 1), "size_bytes must be between")
    refused(BenchmarkRequest(directory=str(tmp_path), size_bytes=10), "size_bytes must be between")
    refused(BenchmarkRequest(directory=str(tmp_path), block_bytes=1), "block_bytes must be between")
    refused(BenchmarkRequest(directory=str(tmp_path / "missing")), "is not a directory")
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    refused(BenchmarkRequest(directory=str(link)), "symbolic link")

    ro = make_service(volumes(volume(str(tmp_path), read_only=True)))
    try:
        ro.start(BenchmarkRequest(directory=str(tmp_path), size_bytes=1024 * 1024))
    except ValueError as exc:
        assert "read-only" in str(exc)
    else:
        raise AssertionError("read-only mount must be refused")

    tight = make_service(volumes(volume("/")), free=1024 * 1024)
    try:
        tight.start(BenchmarkRequest(directory=str(tmp_path), size_bytes=1024 * 1024))
    except ValueError as exc:
        assert "not enough free space" in str(exc)
    else:
        raise AssertionError("insufficient headroom must be refused")


def test_only_one_job_runs_at_a_time(tmp_path: Path) -> None:
    service = make_service(volumes(volume("/")))
    first = service.start(BenchmarkRequest(directory=str(tmp_path), size_bytes=8 * 1024 * 1024, block_bytes=64 * 1024))
    try:
        service.start(BenchmarkRequest(directory=str(tmp_path), size_bytes=1024 * 1024))
    except ValueError as exc:
        assert "already running" in str(exc)
    else:
        raise AssertionError("second concurrent job must be refused")
    service.wait()
    assert service.get(first.id).status == BenchmarkStatus.COMPLETED
    assert service.snapshot().running is False


def test_snapshot_reports_capability_and_history(tmp_path: Path) -> None:
    linux = make_service(volumes(volume("/")), system="Linux")
    assert linux.snapshot().status == CapabilityStatus.PARTIAL
    darwin = make_service(volumes(volume("/")))
    snap = darwin.snapshot()
    assert snap.status == CapabilityStatus.AVAILABLE
    assert snap.default_size_bytes == 64 * 1024 * 1024
    assert snap.items == []

    job = darwin.start(BenchmarkRequest(directory=str(tmp_path), size_bytes=1024 * 1024))
    darwin.wait()
    assert [item.id for item in darwin.snapshot().items] == [job.id]
