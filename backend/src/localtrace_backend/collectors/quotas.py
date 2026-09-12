"""Cached, current-user native quota discovery for macOS.

macOS ``quota -uv`` reports 1 KiB block values followed by inode/file values.
Return codes describe command health; starred usage fields describe overage.
"""

from __future__ import annotations

import getpass
import os
import platform
import pwd
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import psutil

from localtrace_backend.models import (
    CapabilityStatus,
    FilesystemFamily,
    QuotaEntry,
    QuotasResponse,
    VolumesResponse,
)


QUOTA_PATH = "/usr/bin/quota"
QUOTA_TIMEOUT_SECONDS = 2.0
QUOTA_CACHE_TTL_SECONDS = 30.0
MAX_OUTPUT_BYTES = 256 * 1024
SOURCE = f"{QUOTA_PATH} -uv"
NFS4_SEMANTICS_NOTE = (
    "NFSv4 quota availability values are shown as reported by macOS; "
    "LocalTrace does not derive a utilization percentage from them."
)


class CompletedQuotaCommand(Protocol):
    returncode: int
    stdout: bytes | str
    stderr: bytes | str


QuotaRunner = Callable[[Sequence[str], float], CompletedQuotaCommand]


@dataclass(frozen=True)
class ParsedQuotaRow:
    filesystem: str
    used_blocks: int
    soft_limit_blocks: int
    hard_limit_blocks: int
    files_used: int
    file_soft_limit: int
    file_hard_limit: int
    block_over: bool
    file_over: bool
    block_grace: str | None
    file_grace: str | None


@dataclass(frozen=True)
class ParsedQuotaOutput:
    rows: list[ParsedQuotaRow]
    malformed_rows: int


@dataclass(frozen=True)
class _CachedQuota:
    expires_at: float
    response: QuotasResponse


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_message(value: object, limit: int = 240) -> str:
    return " ".join(str(value).split())[:limit]


def _decode(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return value[:MAX_OUTPUT_BYTES]


def run_quota(argv: Sequence[str], timeout_seconds: float) -> CompletedQuotaCommand:
    """Execute an absolute-path command as argv, never through a shell."""

    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        timeout=timeout_seconds,
        shell=False,
        env=environment,
    )


def _current_user() -> tuple[str, int | None]:
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - collector is macOS-only
        uid = None
    if uid is not None:
        try:
            return pwd.getpwuid(uid).pw_name, uid
        except (KeyError, OSError):
            pass
    return getpass.getuser(), uid


_QUOTA_ROW_PATTERNS = (
    # Apple prints grace only for a starred (over-quota) block/file value.
    (True, True, re.compile(
        r"^(?P<filesystem>.+?)\s+(?P<used>\d+)\*\s+(?P<soft>\d+)\s+(?P<hard>\d+)\s+(?P<bgrace>\S+)\s+"
        r"(?P<files>\d+)\*\s+(?P<fsoft>\d+)\s+(?P<fhard>\d+)\s+(?P<fgrace>\S+)\s*$"
    )),
    (True, False, re.compile(
        r"^(?P<filesystem>.+?)\s+(?P<used>\d+)\*\s+(?P<soft>\d+)\s+(?P<hard>\d+)\s+(?P<bgrace>\S+)\s+"
        r"(?P<files>\d+)\s+(?P<fsoft>\d+)\s+(?P<fhard>\d+)\s*$"
    )),
    (False, True, re.compile(
        r"^(?P<filesystem>.+?)\s+(?P<used>\d+)\s+(?P<soft>\d+)\s+(?P<hard>\d+)\s+"
        r"(?P<files>\d+)\*\s+(?P<fsoft>\d+)\s+(?P<fhard>\d+)\s+(?P<fgrace>\S+)\s*$"
    )),
    (False, False, re.compile(
        r"^(?P<filesystem>.+?)\s+(?P<used>\d+)\s+(?P<soft>\d+)\s+(?P<hard>\d+)\s+"
        r"(?P<files>\d+)\s+(?P<fsoft>\d+)\s+(?P<fhard>\d+)\s*$"
    )),
)


def _parse_row(line: str) -> ParsedQuotaRow | None:
    for block_over, file_over, pattern in _QUOTA_ROW_PATTERNS:
        match = pattern.match(line)
        if match is None:
            continue
        values = match.groupdict()
        return ParsedQuotaRow(
            filesystem=values["filesystem"].strip(),
            used_blocks=int(values["used"]),
            soft_limit_blocks=int(values["soft"]),
            hard_limit_blocks=int(values["hard"]),
            files_used=int(values["files"]),
            file_soft_limit=int(values["fsoft"]),
            file_hard_limit=int(values["fhard"]),
            block_over=block_over,
            file_over=file_over,
            block_grace=values.get("bgrace"),
            file_grace=values.get("fgrace"),
        )
    return None


def parse_quota_output(output: str) -> ParsedQuotaOutput:
    """Parse native macOS quota rows without relying on localized headings.

    A long filesystem identifier printed on its own line is joined to the next
    numeric line, which also makes recorded terminal fixtures resilient to
    wrapping. Unrecognized prose is ignored; data-looking malformed rows are
    counted so callers can report partial/error state rather than invent data.
    """

    rows: list[ParsedQuotaRow] = []
    malformed = 0
    pending_filesystem: str | None = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        first = line.split(maxsplit=1)[0].casefold().rstrip(":")
        if first in {"filesystem", "disk", "quota", "warning"}:
            continue

        candidate = f"{pending_filesystem} {line}" if pending_filesystem else line
        parsed = _parse_row(candidate)
        if parsed is not None:
            rows.append(parsed)
            pending_filesystem = None
            continue

        # A numeric-only suffix is likely a wrapped or malformed data row. A
        # nonnumeric line can be a wrapped filesystem/mount name (including
        # spaces, Unicode, colons, and digits), so retain it for the next line.
        numeric_tokens = sum(
            token.rstrip("*").isdecimal() for token in line.split()
        )
        if numeric_tokens >= 3:
            malformed += 1
            pending_filesystem = None
        elif line.startswith("/"):
            pending_filesystem = line

    if pending_filesystem is not None:
        malformed += 1

    return ParsedQuotaOutput(rows=rows, malformed_rows=malformed)


def _limit_bytes(blocks: int) -> int | None:
    return blocks * 1024 if blocks > 0 else None


def _limit(value: int) -> int | None:
    return value if value > 0 else None


def reconcile_quota_semantics(
    quotas: QuotasResponse, volumes: VolumesResponse
) -> QuotasResponse:
    """Resolve generic NFS quota semantics from exact negotiated mount versions.

    macOS may label every NFS mount simply as ``nfs``. The volume collector's
    current ``nfsstat`` evidence is therefore the authoritative input here.
    Version ranges and malformed/absent versions stay unknown: they do not
    prove which protocol was negotiated. The returned response is a copy, so a
    cached native collector snapshot is never mutated.
    """

    nfs_versions_by_mount = {
        os.path.normpath(volume.mount_point): volume.nfs.protocol_version
        for volume in volumes.items
        if volume.filesystem_family == FilesystemFamily.NFS
        and volume.nfs is not None
        and volume.nfs.protocol_version is not None
    }
    reconciled_items: list[QuotaEntry] = []
    found_nfs4 = False
    for item in quotas.items:
        semantics = item.limit_semantics
        if (
            item.filesystem_family == FilesystemFamily.NFS
            and semantics == "unknown"
        ):
            quota_mount = item.mount_point or item.filesystem
            version = nfs_versions_by_mount.get(os.path.normpath(quota_mount))
            if version and re.fullmatch(r"4(?:\.\d+)?", version.strip()):
                semantics = "remaining_availability"
                found_nfs4 = True
            elif version and re.fullmatch(r"[23](?:\.\d+)?", version.strip()):
                semantics = "absolute_limit"
        reconciled_items.append(item.model_copy(update={"limit_semantics": semantics}))

    message = quotas.message
    if found_nfs4 and NFS4_SEMANTICS_NOTE not in (message or ""):
        message = " ".join(part for part in (message, NFS4_SEMANTICS_NOTE) if part)
    return quotas.model_copy(update={"message": message, "items": reconciled_items})


class QuotaCollector:
    """Collect native quotas for only the process's current user."""

    def __init__(
        self,
        *,
        runner: QuotaRunner = run_quota,
        quota_path: str = QUOTA_PATH,
        system_provider: Callable[[], str] = platform.system,
        user_provider: Callable[[], tuple[str, int | None]] = _current_user,
        partitions_provider: Callable[..., Sequence[object]] = psutil.disk_partitions,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        cache_ttl_seconds: float = QUOTA_CACHE_TTL_SECONDS,
    ) -> None:
        if not os.path.isabs(quota_path):
            raise ValueError("quota_path must be absolute")
        self._runner = runner
        self._quota_path = quota_path
        self._system_provider = system_provider
        self._user_provider = user_provider
        self._partitions_provider = partitions_provider
        self._now = now
        self._monotonic = monotonic
        self._cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self._cache: _CachedQuota | None = None
        self._lock = threading.Lock()

    def _response(
        self,
        *,
        status: CapabilityStatus,
        user: str,
        uid: int | None,
        message: str,
        items: list[QuotaEntry] | None = None,
    ) -> QuotasResponse:
        return QuotasResponse(
            sampled_at=self._now(),
            status=status,
            source=f"{self._quota_path} -uv",
            message=message,
            user=user,
            uid=uid,
            items=items or [],
        )

    def _mount_points(self) -> dict[str, tuple[str, str]]:
        try:
            partitions = self._partitions_provider(all=True)
        except (OSError, psutil.Error):
            return {}
        result: dict[str, tuple[str, str]] = {}
        for partition in partitions:
            device = getattr(partition, "device", None)
            mountpoint = getattr(partition, "mountpoint", None)
            filesystem = getattr(partition, "fstype", None)
            if isinstance(device, str) and isinstance(mountpoint, str):
                family = filesystem.casefold() if isinstance(filesystem, str) else "unknown"
                result[device] = (mountpoint, family)
                # Apple quota prints f_mntonname (the mountpoint), not the raw
                # device. Mapping both keeps fixtures and OS variants honest.
                result[mountpoint] = (mountpoint, family)
        return result

    def _collect_uncached(self) -> QuotasResponse:
        user, uid = self._user_provider()
        if self._system_provider() != "Darwin":
            return self._response(
                status=CapabilityStatus.UNAVAILABLE,
                user=user,
                uid=uid,
                message="Native current-user quota collection is currently macOS-only.",
            )

        argv = [self._quota_path, "-uv"]
        try:
            completed = self._runner(argv, QUOTA_TIMEOUT_SECONDS)
        except FileNotFoundError:
            return self._response(
                status=CapabilityStatus.UNAVAILABLE,
                user=user,
                uid=uid,
                message=f"The native quota command was not found at {self._quota_path}.",
            )
        except subprocess.TimeoutExpired:
            return self._response(
                status=CapabilityStatus.ERROR,
                user=user,
                uid=uid,
                message=f"Native quota collection exceeded the {QUOTA_TIMEOUT_SECONDS:g}-second timeout.",
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            return self._response(
                status=CapabilityStatus.ERROR,
                user=user,
                uid=uid,
                message=f"Native quota collection failed: {_clean_message(exc)}",
            )

        stdout = _decode(completed.stdout)
        stderr = _clean_message(_decode(completed.stderr))
        parsed = parse_quota_output(stdout)
        mount_points = self._mount_points()
        items: list[QuotaEntry] = []
        contains_nfs4 = False
        for row in parsed.rows:
            limits = (
                row.soft_limit_blocks,
                row.hard_limit_blocks,
                row.file_soft_limit,
                row.file_hard_limit,
            )
            if not any(limit > 0 for limit in limits):
                continue
            mount_metadata = mount_points.get(row.filesystem)
            filesystem_family = FilesystemFamily.OTHER
            if mount_metadata and mount_metadata[1] == "apfs":
                filesystem_family = FilesystemFamily.APFS
            elif mount_metadata and mount_metadata[1] in {"nfs", "nfs4"}:
                filesystem_family = FilesystemFamily.NFS
                contains_nfs4 = contains_nfs4 or mount_metadata[1] == "nfs4"
            limit_semantics = "unknown"
            if filesystem_family == FilesystemFamily.APFS:
                limit_semantics = "absolute_limit"
            elif mount_metadata and mount_metadata[1] == "nfs4":
                limit_semantics = "remaining_availability"
            items.append(
                QuotaEntry(
                    user=user,
                    uid=uid,
                    filesystem=row.filesystem,
                    filesystem_family=filesystem_family,
                    limit_semantics=limit_semantics,
                    mount_point=mount_metadata[0] if mount_metadata else None,
                    used_bytes=row.used_blocks * 1024,
                    soft_limit_bytes=_limit_bytes(row.soft_limit_blocks),
                    hard_limit_bytes=_limit_bytes(row.hard_limit_blocks),
                    files_used=row.files_used,
                    file_soft_limit=_limit(row.file_soft_limit),
                    file_hard_limit=_limit(row.file_hard_limit),
                    block_over_limit=row.block_over,
                    file_over_limit=row.file_over,
                    block_grace=row.block_grace,
                    file_grace=row.file_grace,
                )
            )

        if items:
            notes = ["Native current-user quota limits reported by macOS."]
            status = CapabilityStatus.AVAILABLE
            if any(row.block_over or row.file_over for row in parsed.rows):
                notes.append("At least one reported quota is currently exceeded.")
            if completed.returncode != 0:
                status = CapabilityStatus.PARTIAL
                notes.append(f"quota also exited with status {completed.returncode}.")
            if parsed.malformed_rows:
                status = CapabilityStatus.PARTIAL
                notes.append(f"{parsed.malformed_rows} malformed row(s) were ignored.")
            if stderr:
                status = CapabilityStatus.PARTIAL
                notes.append(f"quota reported: {stderr}")
            if contains_nfs4:
                notes.append(NFS4_SEMANTICS_NOTE)
            return self._response(
                status=status,
                user=user,
                uid=uid,
                message=" ".join(notes),
                items=items,
            )

        empty_result_diagnostics: list[str] = []
        if completed.returncode != 0:
            empty_result_diagnostics.append(
                f"quota also exited with status {completed.returncode}."
            )
        if parsed.malformed_rows:
            empty_result_diagnostics.append(
                f"{parsed.malformed_rows} malformed row(s) were ignored."
            )
        if stderr:
            empty_result_diagnostics.append(f"quota reported: {stderr}")

        if parsed.rows:
            notes = [
                "No reportable nonzero native current-user quota limit was reported."
            ]
            notes.extend(empty_result_diagnostics)
            return self._response(
                status=(
                    CapabilityStatus.PARTIAL
                    if empty_result_diagnostics
                    else CapabilityStatus.AVAILABLE
                ),
                user=user,
                uid=uid,
                message=" ".join(notes),
            )

        output_says_none = any(
            line.strip().casefold().endswith(" none")
            or line.strip().casefold().endswith(": none")
            for line in stdout.splitlines()
        )
        if output_says_none:
            notes = [
                "No reportable nonzero native current-user quota record was returned; "
                "this does not prove the filesystem lacks quota support."
            ]
            notes.extend(empty_result_diagnostics)
            return self._response(
                status=(
                    CapabilityStatus.PARTIAL
                    if empty_result_diagnostics
                    else CapabilityStatus.AVAILABLE
                ),
                user=user,
                uid=uid,
                message=" ".join(notes),
            )

        details = " ".join(empty_result_diagnostics) or (
            "quota returned no recognizable data rows."
        )
        return self._response(
            status=CapabilityStatus.ERROR,
            user=user,
            uid=uid,
            message=f"Native quota output could not be used: {details}",
        )

    def collect(self) -> QuotasResponse:
        """Return a cached snapshot and serialize refreshes across API requests."""

        with self._lock:
            current_time = self._monotonic()
            if self._cache is not None and current_time < self._cache.expires_at:
                return self._cache.response
            response = self._collect_uncached()
            self._cache = _CachedQuota(
                expires_at=current_time + self._cache_ttl_seconds,
                response=response,
            )
            return response
