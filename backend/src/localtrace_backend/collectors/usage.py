"""Per-owner disk usage across the directories LocalTrace is watching.

The scanner answers the administrator question "who is consuming the model
storage on this Mac?" without any filesystem quota support. It walks each
watched directory in a background thread, groups file bytes by ``st_uid``,
and keeps the newest completed result for the API to serve without blocking.

Accuracy rules:

- ``apparent_bytes`` is the sum of ``st_size``. ``allocated_bytes`` is the sum
  of ``st_blocks * 512`` and is an upper bound on APFS: clones and sparse files
  can share or skip blocks, so allocated bytes may overstate unique storage.
- Symbolic links are never followed. Hugging Face hub snapshots link into
  ``blobs``; counting the link target once through the blob keeps totals true.
- Hard links are counted once per ``(st_dev, st_ino)`` within a scan.
- File ownership at scan time is evidence for investigation, not proof of
  which process or person wrote the file.
- A scan that hits its file or time budget is ``partial`` with a message; it
  never reports a truncated total as a complete one. The clock is checked on
  every directory entry, not only per counted file, but a single ``stat``
  blocked inside the kernel (a stale hard NFS mount) cannot be interrupted.
- More owners than the table can show is also ``partial``; ``owner_count``
  always carries the true number.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from localtrace_backend.models import (
    CapabilityStatus,
    OwnerUsage,
    UsageFile,
    UsageResponse,
    UsageTarget,
    UsageTargetStatus,
)

SOURCE = "os.scandir(follow_symlinks=False)+os.lstat"
DEFAULT_SCAN_INTERVAL_SECONDS = 60.0
DEFAULT_MAX_FILES = 200_000
DEFAULT_MAX_SECONDS = 20.0
DEFAULT_TOP_FILES = 3
MAX_OWNERS = 64


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_message(value: object, limit: int = 240) -> str:
    return " ".join(str(value).split())[:limit]


def _entry_stat(entry: os.DirEntry[str]) -> os.stat_result:
    """Stat one directory entry without following symbolic links."""

    return entry.stat(follow_symlinks=False)


def _owner_name(uid: int) -> str | None:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, OSError):
        return None


@dataclass
class _OwnerAccumulator:
    uid: int
    file_count: int = 0
    apparent_bytes: int = 0
    allocated_bytes: int = 0
    top_files: list[UsageFile] = field(default_factory=list)

    def add(self, path: str, apparent: int, allocated: int, top_limit: int) -> None:
        self.file_count += 1
        self.apparent_bytes += apparent
        self.allocated_bytes += allocated
        if top_limit <= 0:
            return
        entry = UsageFile(path=path, apparent_bytes=apparent, allocated_bytes=allocated)
        if len(self.top_files) < top_limit:
            self.top_files.append(entry)
            self.top_files.sort(key=lambda item: item.apparent_bytes, reverse=True)
        elif apparent > self.top_files[-1].apparent_bytes:
            self.top_files[-1] = entry
            self.top_files.sort(key=lambda item: item.apparent_bytes, reverse=True)


class _Budget:
    def __init__(self, max_files: int, max_seconds: float, monotonic: Callable[[], float]):
        self._max_files = max_files
        self._deadline = monotonic() + max_seconds
        self._monotonic = monotonic
        self.files = 0
        self.exhausted_reason: str | None = None

    def expired(self) -> bool:
        """Check the clock without counting a file; used on every entry."""

        if self.exhausted_reason is not None:
            return True
        if self._monotonic() > self._deadline:
            self.exhausted_reason = "time budget reached"
            return True
        return False

    def consume(self) -> bool:
        """Record one file; return False once the budget is exhausted."""

        self.files += 1
        if self.files > self._max_files:
            self.exhausted_reason = f"file budget of {self._max_files} reached"
            return False
        return not self.expired()


class DiskUsageScanner:
    """Own a background scan loop and the newest completed usage snapshot."""

    def __init__(
        self,
        *,
        directories_provider: Callable[[], Sequence[str]],
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        max_files: int = DEFAULT_MAX_FILES,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        top_files: int = DEFAULT_TOP_FILES,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        owner_name: Callable[[int], str | None] = _owner_name,
    ) -> None:
        if scan_interval_seconds <= 0 or max_files <= 0 or max_seconds <= 0:
            raise ValueError("scan interval, file budget, and time budget must be positive")
        self._directories_provider = directories_provider
        self.scan_interval_seconds = scan_interval_seconds
        self.max_files = max_files
        self.max_seconds = max_seconds
        self.top_files = max(0, top_files)
        self._now = now
        self._monotonic = monotonic
        self._owner_name = owner_name
        self._lock = threading.Lock()
        self._latest: UsageResponse | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_environment(
        cls, directories_provider: Callable[[], Sequence[str]]
    ) -> "DiskUsageScanner":
        def read_positive(name: str, default: float) -> float:
            raw = os.getenv(name)
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                return default
            return value if value > 0 else default

        return cls(
            directories_provider=directories_provider,
            scan_interval_seconds=read_positive(
                "LOCALTRACE_USAGE_SCAN_INTERVAL_SECONDS", DEFAULT_SCAN_INTERVAL_SECONDS
            ),
            max_files=int(read_positive("LOCALTRACE_USAGE_MAX_FILES", DEFAULT_MAX_FILES)),
            max_seconds=read_positive("LOCALTRACE_USAGE_MAX_SECONDS", DEFAULT_MAX_SECONDS),
        )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._loop, name="localtrace-usage-scan", daemon=True
            )
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_now()
            except Exception as exc:  # a scan bug must not kill the loop
                with self._lock:
                    self._latest = self._response(
                        status=CapabilityStatus.ERROR,
                        message=f"Usage scan failed: {_clean_message(exc)}",
                        directories=[],
                        owners=[],
                        started_at=self._now(),
                        duration=0.0,
                        truncated=False,
                    )
            self._stop.wait(self.scan_interval_seconds)

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> UsageResponse:
        with self._lock:
            latest = self._latest
        if latest is not None:
            return latest
        return self._response(
            status=CapabilityStatus.WARMING_UP,
            message="The first usage scan has not completed yet.",
            directories=[],
            owners=[],
            started_at=None,
            duration=0.0,
            truncated=False,
        )

    def _response(
        self,
        *,
        status: CapabilityStatus,
        message: str | None,
        directories: list[UsageTarget],
        owners: list[OwnerUsage],
        started_at: datetime | None,
        duration: float,
        truncated: bool,
        owner_count: int | None = None,
    ) -> UsageResponse:
        return UsageResponse(
            sampled_at=self._now(),
            status=status,
            source=SOURCE,
            message=message,
            scan_started_at=started_at,
            scan_duration_seconds=round(max(0.0, duration), 3),
            scan_interval_seconds=self.scan_interval_seconds,
            truncated=truncated,
            file_count=sum(item.file_count for item in directories),
            total_apparent_bytes=sum(item.apparent_bytes for item in directories),
            total_allocated_bytes=sum(item.allocated_bytes for item in directories),
            owner_count=len(owners) if owner_count is None else owner_count,
            directories=directories,
            owners=owners,
        )

    # -- scanning ----------------------------------------------------------

    def scan_now(self) -> UsageResponse:
        """Run one complete scan synchronously and publish the result."""

        started_at = self._now()
        started = self._monotonic()
        directories = [
            os.path.abspath(os.path.expanduser(path))
            for path in self._directories_provider()
        ]
        if not directories:
            result = self._response(
                status=CapabilityStatus.UNAVAILABLE,
                message="No directory is currently being watched, so there is nothing to scan.",
                directories=[],
                owners=[],
                started_at=started_at,
                duration=self._monotonic() - started,
                truncated=False,
            )
            with self._lock:
                self._latest = result
            return result

        budget = _Budget(self.max_files, self.max_seconds, self._monotonic)
        seen: set[tuple[int, int]] = set()
        owners: dict[int, _OwnerAccumulator] = {}
        targets: list[UsageTarget] = []
        for directory in directories:
            targets.append(self._scan_directory(directory, budget, seen, owners))
            if budget.exhausted_reason is not None:
                remaining = directories[len(targets):]
                targets.extend(
                    UsageTarget(
                        path=path,
                        status=UsageTargetStatus.PARTIAL,
                        file_count=0,
                        apparent_bytes=0,
                        allocated_bytes=0,
                        message=f"Not scanned: {budget.exhausted_reason}.",
                    )
                    for path in remaining
                )
                break

        total_apparent = sum(item.apparent_bytes for item in owners.values())
        owner_count = len(owners)
        owner_rows = [
            OwnerUsage(
                uid=item.uid,
                owner_name=self._owner_name(item.uid),
                file_count=item.file_count,
                apparent_bytes=item.apparent_bytes,
                allocated_bytes=item.allocated_bytes,
                share_percent=round(
                    (item.apparent_bytes / total_apparent) * 100 if total_apparent else 0.0,
                    2,
                ),
                top_files=list(item.top_files),
            )
            for item in sorted(
                owners.values(), key=lambda item: item.apparent_bytes, reverse=True
            )
        ][:MAX_OWNERS]

        truncated = budget.exhausted_reason is not None
        errored = [target for target in targets if target.status == UsageTargetStatus.ERROR]
        partial = [target for target in targets if target.status == UsageTargetStatus.PARTIAL]
        messages: list[str] = []
        if truncated:
            messages.append(
                f"Scan stopped early ({budget.exhausted_reason}); totals cover only the "
                "files visited."
            )
        if errored:
            messages.append(f"{len(errored)} director{'y' if len(errored) == 1 else 'ies'} could not be read.")
        elif partial and not truncated:
            messages.append("Some entries could not be read; see directories.")
        if owner_count > MAX_OWNERS:
            messages.append(
                f"Showing the top {MAX_OWNERS} of {owner_count} owners by apparent bytes."
            )
        if errored and len(errored) == len(targets):
            status = CapabilityStatus.ERROR
        elif messages:
            status = CapabilityStatus.PARTIAL
        else:
            status = CapabilityStatus.AVAILABLE
            messages.append(
                "File ownership is evidence, not writer identity. Allocated bytes are an "
                "upper bound on APFS because clones and sparse files can share blocks."
            )

        result = self._response(
            status=status,
            message=" ".join(messages) or None,
            directories=targets,
            owners=owner_rows,
            owner_count=owner_count,
            started_at=started_at,
            duration=self._monotonic() - started,
            truncated=truncated,
        )
        with self._lock:
            self._latest = result
        return result

    def _scan_directory(
        self,
        root: str,
        budget: _Budget,
        seen: set[tuple[int, int]],
        owners: dict[int, _OwnerAccumulator],
    ) -> UsageTarget:
        file_count = 0
        apparent_total = 0
        allocated_total = 0
        unreadable = 0
        stack = [root]
        try:
            root_stat = os.lstat(root)
        except OSError as exc:
            return UsageTarget(
                path=root,
                status=UsageTargetStatus.ERROR,
                file_count=0,
                apparent_bytes=0,
                allocated_bytes=0,
                message=_clean_message(exc),
            )
        if not os.path.isdir(root) or os.path.islink(root):
            return UsageTarget(
                path=root,
                status=UsageTargetStatus.ERROR,
                file_count=0,
                apparent_bytes=0,
                allocated_bytes=0,
                message="Not a real directory.",
            )
        del root_stat

        while stack:
            if budget.expired():
                break
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if budget.expired():
                            break
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            info = _entry_stat(entry)
                        except OSError:
                            unreadable += 1
                            continue
                        key = (int(info.st_dev), int(info.st_ino))
                        if key in seen:
                            continue
                        seen.add(key)
                        if not budget.consume():
                            break
                        apparent = max(0, int(info.st_size))
                        blocks = getattr(info, "st_blocks", None)
                        allocated = (
                            max(0, int(blocks)) * 512 if blocks is not None else apparent
                        )
                        file_count += 1
                        apparent_total += apparent
                        allocated_total += allocated
                        uid = int(info.st_uid)
                        accumulator = owners.get(uid)
                        if accumulator is None:
                            accumulator = owners[uid] = _OwnerAccumulator(uid=uid)
                        accumulator.add(entry.path, apparent, allocated, self.top_files)
            except OSError:
                unreadable += 1

        if budget.exhausted_reason is not None:
            status = UsageTargetStatus.PARTIAL
            message = f"Stopped early: {budget.exhausted_reason}."
        elif unreadable:
            status = UsageTargetStatus.PARTIAL
            message = f"{unreadable} entr{'y' if unreadable == 1 else 'ies'} could not be read."
        else:
            status = UsageTargetStatus.SCANNED
            message = None
        return UsageTarget(
            path=root,
            status=status,
            file_count=file_count,
            apparent_bytes=apparent_total,
            allocated_bytes=allocated_total,
            message=message,
        )
