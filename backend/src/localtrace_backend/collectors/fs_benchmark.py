"""Filesystem-specific throughput measured by a bounded, on-demand probe.

macOS exposes byte counters per physical device (see ``collectors/io.py``)
but not per APFS volume or NFS mount without privileged tracing. The only
honest way to report GB/s for a *filesystem* is to measure it: write a
temporary file on the chosen mount, flush it, read it back, and time each
phase. That is what this module does, on request, one job at a time, with
the file removed afterwards.

Accuracy rules:

- A run is a single sequential write followed by a single sequential read at
  one block size. It characterizes that path on that mount at that moment;
  it is not a benchmark suite and is labeled as such.
- On macOS the file is opened with ``F_NOCACHE`` so the read phase measures
  the filesystem rather than the unified buffer cache, and the write phase
  ends with ``F_FULLFSYNC`` so the bytes are on stable storage before the
  clock stops. Both facts are recorded on the result; when either is not
  available the result says so instead of pretending.
- Write throughput includes flush time. Flush time is also reported alone.
- The mount attributed to a run is the deepest mount point that contains the
  resolved directory, so writing under ``/tmp`` on a sealed-system Mac is
  attributed to the Data volume, not ``/``.

Safety rules:

- The file is created ``O_CREAT | O_EXCL | O_NOFOLLOW`` with mode ``0600``
  under a dedicated ``localtrace-benchmark`` directory, and is removed in a
  ``finally`` block.
- Symbolic links anywhere on the directory path are refused; read-only
  mounts are refused; a run needs 20% headroom over its size in free space.
- Size is capped by ``LOCALTRACE_BENCHMARK_MAX_BYTES`` (2 GiB default).
- Only one job runs at a time.
"""

from __future__ import annotations

import errno
import os
import platform
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from localtrace_backend.models import (
    BenchmarkJob,
    BenchmarkPhase,
    BenchmarkPhaseResult,
    BenchmarkRequest,
    BenchmarksResponse,
    BenchmarkStatus,
    CapabilityStatus,
    Volume,
    VolumesResponse,
)

SOURCE = "sequential write+F_FULLFSYNC then sequential read, F_NOCACHE (macOS)"
DEFAULT_SIZE_BYTES = 256 * 1024 * 1024
DEFAULT_BLOCK_BYTES = 4 * 1024 * 1024
MIN_SIZE_BYTES = 1024 * 1024
DEFAULT_MAX_SIZE_BYTES = 2 * 1024 * 1024 * 1024
MIN_BLOCK_BYTES = 64 * 1024
MAX_BLOCK_BYTES = 64 * 1024 * 1024
MAX_JOBS = 20
HEADROOM = 1.2
BENCH_DIRNAME = "localtrace-benchmark"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: object, limit: int = 200) -> str:
    return " ".join(str(value).split())[:limit]


def _gb(bytes_per_second: float) -> float:
    return round(bytes_per_second / 1_000_000_000, 4)


def _refuse_symlinks(path: Path) -> None:
    """Reject a path if it or any ancestor is a symbolic link."""

    for candidate in [path, *path.parents]:
        if candidate.is_symlink():
            raise PermissionError(f"refusing a symbolic link on the path: {candidate}")


def _phase_result(nbytes: int, seconds: float, flush_seconds: float | None) -> BenchmarkPhaseResult:
    rate = nbytes / seconds if seconds > 0 else 0.0
    return BenchmarkPhaseResult(
        bytes=nbytes,
        seconds=round(seconds, 4),
        flush_seconds=None if flush_seconds is None else round(flush_seconds, 4),
        bytes_per_second=round(rate, 2),
        gb_per_second=_gb(rate),
    )


class FilesystemBenchmarkService:
    """Run one throughput job at a time and keep a bounded history."""

    def __init__(
        self,
        *,
        volumes_provider: Callable[[], VolumesResponse | None],
        max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
        system_provider: Callable[[], str] = platform.system,
        disk_usage: Callable[[str], shutil._ntuple_diskusage] = shutil.disk_usage,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_size_bytes < MIN_SIZE_BYTES:
            raise ValueError("max_size_bytes must be at least 1 MiB")
        self._volumes_provider = volumes_provider
        self.max_size_bytes = max_size_bytes
        self._system = system_provider()
        self._disk_usage = disk_usage
        self._now = now
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._jobs: deque[BenchmarkJob] = deque(maxlen=MAX_JOBS)
        self._running: threading.Thread | None = None
        self._progress_bytes = 0
        self._phase = BenchmarkPhase.QUEUED

    @classmethod
    def from_environment(
        cls, volumes_provider: Callable[[], VolumesResponse | None]
    ) -> "FilesystemBenchmarkService":
        raw = os.getenv("LOCALTRACE_BENCHMARK_MAX_BYTES")
        cap = DEFAULT_MAX_SIZE_BYTES
        if raw:
            try:
                parsed = int(raw)
                if parsed >= MIN_SIZE_BYTES:
                    cap = parsed
            except ValueError:
                pass
        return cls(volumes_provider=volumes_provider, max_size_bytes=cap)

    # -- public API ---------------------------------------------------------

    def snapshot(self) -> BenchmarksResponse:
        with self._lock:
            jobs = list(self._jobs)
            running = self._running is not None
            phase = self._phase
            done = self._progress_bytes
        jobs.reverse()
        current = next((job for job in jobs if job.status == BenchmarkStatus.RUNNING), None)
        if current is not None:
            total = current.size_bytes * 2
            phase_offset = current.size_bytes if phase == BenchmarkPhase.READ else 0
            percent = min(100.0, (phase_offset + done) / total * 100) if total else 0.0
            current = current.model_copy(update={"phase": phase, "progress_percent": round(percent, 1)})
            jobs = [current if job.id == current.id else job for job in jobs]
        if self._system != "Darwin":
            status = CapabilityStatus.PARTIAL
            message = (
                "Runs measure the filesystem, but cache bypass (F_NOCACHE) and stable "
                "flush (F_FULLFSYNC) are macOS-only; results here may include cache effects."
            )
        else:
            status = CapabilityStatus.AVAILABLE
            message = (
                "Each run is one sequential write with a stable flush and one sequential "
                "read with the cache bypassed. It characterizes that mount now; it is not "
                "a benchmark suite."
            )
        return BenchmarksResponse(
            sampled_at=self._now(),
            status=status,
            source=SOURCE,
            message=message,
            running=running,
            max_size_bytes=self.max_size_bytes,
            default_size_bytes=min(DEFAULT_SIZE_BYTES, self.max_size_bytes),
            items=jobs,
        )

    def get(self, job_id: str) -> BenchmarkJob | None:
        with self._lock:
            for job in self._jobs:
                if job.id == job_id:
                    return job
        return None

    def start(self, request: BenchmarkRequest) -> BenchmarkJob:
        """Validate and launch a job. Raises ValueError for a refused request."""

        size = request.size_bytes or min(DEFAULT_SIZE_BYTES, self.max_size_bytes)
        block = request.block_bytes or DEFAULT_BLOCK_BYTES
        if not MIN_SIZE_BYTES <= size <= self.max_size_bytes:
            raise ValueError(
                f"size_bytes must be between {MIN_SIZE_BYTES} and {self.max_size_bytes}"
            )
        if not MIN_BLOCK_BYTES <= block <= MAX_BLOCK_BYTES:
            raise ValueError(f"block_bytes must be between {MIN_BLOCK_BYTES} and {MAX_BLOCK_BYTES}")
        if block > size:
            block = size

        directory = self._resolve_directory(request)
        volume = self._volume_for(directory)
        if volume is not None and volume.read_only:
            raise ValueError(f"{volume.mount_point} is mounted read-only")
        try:
            usage = self._disk_usage(str(directory.parent if not directory.exists() else directory))
        except OSError as exc:
            raise ValueError(f"cannot read free space for {directory}: {_clean(exc)}") from exc
        if usage.free < size * HEADROOM:
            raise ValueError(
                f"not enough free space: need {int(size * HEADROOM)} bytes, {usage.free} available"
            )

        with self._lock:
            if self._running is not None:
                raise ValueError("a benchmark is already running")
            job = BenchmarkJob(
                id=uuid.uuid4().hex,
                status=BenchmarkStatus.RUNNING,
                phase=BenchmarkPhase.QUEUED,
                progress_percent=0.0,
                requested_at=self._now(),
                started_at=None,
                finished_at=None,
                directory=str(directory),
                mount_point=volume.mount_point if volume else None,
                filesystem=volume.filesystem if volume else None,
                filesystem_family=volume.filesystem_family if volume else None,
                remote=volume.remote if volume else None,
                size_bytes=size,
                block_bytes=block,
                cache_bypass=False,
                flush_method="none",
                write=None,
                read=None,
                message=None,
            )
            self._jobs.append(job)
            self._progress_bytes = 0
            self._phase = BenchmarkPhase.QUEUED
            thread = threading.Thread(
                target=self._run, args=(job.id, directory, size, block), daemon=True,
                name="localtrace-benchmark",
            )
            self._running = thread
        thread.start()
        return job

    def wait(self, timeout: float = 60.0) -> None:
        """Block until the current job finishes; intended for tests."""

        with self._lock:
            thread = self._running
        if thread is not None:
            thread.join(timeout=timeout)

    # -- internals ---------------------------------------------------------

    def _resolve_directory(self, request: BenchmarkRequest) -> Path:
        if request.directory:
            base = Path(os.path.abspath(os.path.expanduser(request.directory)))
        else:
            mount = os.path.abspath(request.mount_point or "/")
            if mount == "/":
                # The root of a sealed-system Mac is read-only; the temp dir
                # lives on the Data volume and is attributed there.
                base = Path(tempfile.gettempdir())
            else:
                base = Path(mount)
        directory = base / BENCH_DIRNAME
        try:
            _refuse_symlinks(base)
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc
        if not base.is_dir():
            raise ValueError(f"{base} is not a directory")
        if not os.access(base, os.W_OK):
            raise ValueError(f"{base} is not writable by this account")
        return directory

    def _volume_for(self, directory: Path) -> Volume | None:
        latest = self._volumes_provider()
        if latest is None:
            return None
        target = str(directory)
        best: Volume | None = None
        for item in latest.items:
            mount = item.mount_point.rstrip("/") or "/"
            if target == mount or target.startswith(mount + "/") or mount == "/":
                if best is None or len(mount) > len(best.mount_point.rstrip("/") or "/"):
                    best = item
        return best

    def _update(self, job_id: str, **fields: object) -> None:
        with self._lock:
            for index, job in enumerate(self._jobs):
                if job.id == job_id:
                    self._jobs[index] = job.model_copy(update=fields)
                    return

    def _run(self, job_id: str, directory: Path, size: int, block: int) -> None:
        file_path = directory / f"{job_id}.bin"
        fd: int | None = None
        cache_bypass = False
        flush_method = "fsync"
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            _refuse_symlinks(directory)
            self._update(job_id, started_at=self._now(), phase=BenchmarkPhase.WRITE)
            with self._lock:
                self._phase = BenchmarkPhase.WRITE
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(file_path, flags, 0o600)
            nocache = getattr(os, "F_NOCACHE", None)
            if nocache is not None:
                try:
                    import fcntl

                    fcntl.fcntl(fd, nocache, 1)
                    cache_bypass = True
                except OSError:
                    cache_bypass = False
            fullfsync = getattr(os, "F_FULLFSYNC", None)
            if fullfsync is not None:
                flush_method = "F_FULLFSYNC"
            self._update(job_id, cache_bypass=cache_bypass, flush_method=flush_method)

            payload = os.urandom(min(block, 1024 * 1024)) * (max(1, block // (1024 * 1024)))
            payload = payload[:block]
            write_start = self._monotonic()
            remaining = size
            while remaining > 0:
                chunk = payload if remaining >= block else payload[:remaining]
                written = os.write(fd, chunk)
                if written <= 0:
                    raise OSError(errno.EIO, "short write")
                remaining -= written
                with self._lock:
                    self._progress_bytes = size - remaining
            flush_start = self._monotonic()
            if fullfsync is not None:
                import fcntl

                try:
                    fcntl.fcntl(fd, fullfsync)
                except OSError:
                    os.fsync(fd)
                    flush_method = "fsync (F_FULLFSYNC refused)"
            else:
                os.fsync(fd)
            write_end = self._monotonic()
            write_result = _phase_result(size, write_end - write_start, write_end - flush_start)
            self._update(job_id, write=write_result, flush_method=flush_method, phase=BenchmarkPhase.READ)
            with self._lock:
                self._phase = BenchmarkPhase.READ
                self._progress_bytes = 0

            os.lseek(fd, 0, os.SEEK_SET)
            read_start = self._monotonic()
            total_read = 0
            while total_read < size:
                data = os.read(fd, min(block, size - total_read))
                if not data:
                    raise OSError(errno.EIO, "short read")
                total_read += len(data)
                with self._lock:
                    self._progress_bytes = total_read
            read_end = self._monotonic()
            read_result = _phase_result(total_read, read_end - read_start, None)
            self._update(
                job_id,
                status=BenchmarkStatus.COMPLETED,
                phase=BenchmarkPhase.DONE,
                progress_percent=100.0,
                finished_at=self._now(),
                read=read_result,
                message=(
                    "Single sequential run; write includes stable flush."
                    if cache_bypass
                    else "Single sequential run without cache bypass; read may reflect the buffer cache."
                ),
            )
        except Exception as exc:
            self._update(
                job_id,
                status=BenchmarkStatus.FAILED,
                phase=BenchmarkPhase.DONE,
                finished_at=self._now(),
                message=f"Benchmark failed: {_clean(exc)}",
            )
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                if file_path.is_file() and not file_path.is_symlink():
                    file_path.unlink()
            except OSError:
                pass
            with self._lock:
                self._running = None
                self._phase = BenchmarkPhase.QUEUED
                self._progress_bytes = 0
