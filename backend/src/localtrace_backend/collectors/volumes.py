"""Mounted-volume and capacity collection.

Capacity values come from the same kernel-backed APIs used by ``df`` (through
psutil). On macOS, APFS/SMART metadata comes from ``diskutil info -plist`` and
current NFS mount metadata comes from ``nfsstat -f JSON``. Only structured,
allowlisted fields are returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import psutil

from localtrace_backend.models import (
    APFSDetails,
    CapabilityStatus,
    FilesystemFamily,
    NFSDetails,
    Volume,
    VolumeHealth,
    VolumesResponse,
)


DISKUTIL_TIMEOUT_SECONDS = 2.0
DISKUTIL_CACHE_TTL_SECONDS = 45.0
NFSSTAT_TIMEOUT_SECONDS = 2.0
NFSSTAT_CACHE_TTL_SECONDS = 45.0
MAX_MOUNTS = 128

# ``psutil.disk_partitions(all=False)`` is not a usable definition of a
# storage volume on macOS: psutil keeps only sources that are absolute paths,
# which excludes ordinary NFS sources such as ``server:/export``.  Collect all
# mount-table entries, then discard only filesystem types that represent OS
# plumbing rather than storage a user can manage.  This deliberately remains
# a denylist so APFS, NFS, and unfamiliar real filesystems stay visible.
_PSEUDO_FILESYSTEM_TYPES = frozenset(
    {
        "autofs",
        "bpf",
        "binfmt_misc",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fdesc",
        "fusectl",
        "hugetlbfs",
        "kernfs",
        "mqueue",
        "nfsd",
        "nsfs",
        "proc",
        "procfs",
        "pstore",
        "ramfs",
        "rpc_pipefs",
        "securityfs",
        "selinuxfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
)


class PartitionLike(Protocol):
    device: str
    mountpoint: str
    fstype: str
    opts: str


class UsageLike(Protocol):
    total: int
    used: int
    free: int
    percent: float


@dataclass(frozen=True)
class _RootFallback:
    device: str = "/"
    mountpoint: str = "/"
    fstype: str = "unknown"
    opts: str = "rw"


@dataclass(frozen=True)
class _CachedDiskutil:
    expires_at: float
    info: Mapping[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class _NFSStatData:
    server: str | None
    export: str | None
    mount_options: list[str]
    status_flags: list[str]


@dataclass(frozen=True)
class _CachedNFSStat:
    expires_at: float
    info: _NFSStatData | None
    error: str | None


DiskutilRunner = Callable[[str, float], Mapping[str, Any]]
NFSStatRunner = Callable[[str, float], Mapping[str, Any]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_error(value: object, limit: int = 180) -> str:
    text = " ".join(str(value).split())
    return text[:limit]


def diskutil_info_plist(
    target: str, timeout_seconds: float = DISKUTIL_TIMEOUT_SECONDS
) -> Mapping[str, Any]:
    """Return structured disk metadata, bounded by a short timeout.

    ``target`` is passed as a single argument and no shell is involved.
    """

    try:
        completed = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", target],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("diskutil is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"diskutil did not respond within {timeout_seconds:g} seconds"
        ) from exc

    if completed.returncode != 0:
        reason = _clean_error(completed.stderr.decode("utf-8", errors="replace"))
        raise RuntimeError(reason or f"diskutil exited with {completed.returncode}")

    try:
        value = plistlib.loads(completed.stdout)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise RuntimeError("diskutil returned an invalid property list") from exc
    if not isinstance(value, dict):
        raise RuntimeError("diskutil returned an unexpected property-list root")
    return value


def nfsstat_mount_json(
    target: str, timeout_seconds: float = NFSSTAT_TIMEOUT_SECONDS
) -> Mapping[str, Any]:
    """Return Apple nfsstat's structured current mount information safely."""

    try:
        completed = subprocess.run(
            ["/usr/bin/nfsstat", "-v", "-f", "JSON", "-m", target],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("nfsstat is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"nfsstat did not respond within {timeout_seconds:g} seconds"
        ) from exc
    if completed.returncode != 0:
        reason = _clean_error(completed.stderr.decode("utf-8", errors="replace"))
        raise RuntimeError(reason or f"nfsstat exited with {completed.returncode}")
    if len(completed.stdout) > 512 * 1024:
        raise RuntimeError("nfsstat returned unexpectedly large JSON")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("nfsstat returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("nfsstat returned an unexpected JSON root")
    return value


def _filesystem_family(fstype: str) -> FilesystemFamily:
    normalized = fstype.strip().lower()
    if normalized == "apfs":
        return FilesystemFamily.APFS
    if normalized in {"nfs", "nfs4"}:
        return FilesystemFamily.NFS
    return FilesystemFamily.OTHER


def _is_storage_filesystem(fstype: str) -> bool:
    """Keep mounted storage while excluding known kernel/pseudo filesystems."""

    return fstype.strip().casefold() not in _PSEUDO_FILESYSTEM_TYPES


def _is_remote(device: str, fstype: str) -> bool:
    normalized = fstype.strip().lower()
    return normalized in {
        "nfs",
        "nfs4",
        "smbfs",
        "cifs",
        "afpfs",
        "sshfs",
        "webdav",
    } or (":" in device and not device.startswith("/dev/"))


def _mount_options(raw_options: str) -> list[str]:
    """Return deterministic, de-duplicated mount options without inventing any."""

    unique: dict[str, str] = {}
    for raw in raw_options.split(","):
        option = raw.strip()
        if option:
            unique.setdefault(option.casefold(), option)
    return sorted(unique.values(), key=lambda value: (value.casefold(), value))


def _nfs_server_export(device: str) -> tuple[str | None, str | None]:
    """Parse common NFS sources, including bracketed IPv6, conservatively."""

    value = device.strip()
    if value.casefold().startswith("nfs://"):
        parsed = urlsplit(value)
        server = parsed.hostname
        export = parsed.path or "/"
        return (server or None, export)

    # NFS sources use ``server:/export``. Splitting on the first colon would
    # corrupt bracketed IPv6 literals, so locate the colon-slash delimiter.
    delimiter = value.find(":/")
    if delimiter <= 0:
        return None, None
    server = value[:delimiter].strip()
    if server.startswith("[") and server.endswith("]"):
        server = server[1:-1]
    export = value[delimiter + 1 :]
    return (server or None, export or None)


def _nfs_protocol_version(filesystem: str, options: Sequence[str]) -> str | None:
    explicit_version: str | None = None
    minor_version: str | None = None
    for option in options:
        normalized = option.casefold()
        for prefix in ("vers=", "nfsvers="):
            if normalized.startswith(prefix):
                value = option[len(prefix) :].strip()
                if value:
                    explicit_version = value
        if normalized.startswith("minorversion="):
            value = option.split("=", 1)[1].strip()
            if value:
                minor_version = value
        if normalized in {"nfsv2", "nfsv3", "nfsv4"}:
            explicit_version = normalized.removeprefix("nfsv")

    if explicit_version == "4" and minor_version:
        return f"4.{minor_version}"
    if explicit_version:
        return explicit_version
    # The filesystem label itself can explicitly identify NFSv4. It still
    # says nothing about pNFS negotiation.
    if filesystem.casefold() == "nfs4":
        return "4"
    return None


def _pnfs_capability(
    system: str, protocol_version: str | None, options: Sequence[str]
) -> tuple[CapabilityStatus, str]:
    if system == "Darwin":
        return (
            CapabilityStatus.UNAVAILABLE,
            "The current macOS NFS client explicitly does not support pNFS; NFSv4.1 alone is not pNFS evidence.",
        )
    normalized = [option.casefold() for option in options]
    disabled = any(
        option == "nopnfs"
        or option in {
            "pnfs=0",
            "pnfs=no",
            "pnfs=off",
            "pnfs=false",
            "pnfs=disabled",
        }
        for option in normalized
    )
    if disabled:
        return (
            CapabilityStatus.UNAVAILABLE,
            "A mount option explicitly disables pNFS.",
        )

    evidence = [
        option
        for option in options
        if option.casefold() == "pnfs"
        or option.casefold().startswith("layouttype=")
        or option.casefold().startswith("pnfs=")
    ]
    if evidence:
        return (
            CapabilityStatus.AVAILABLE,
            "Explicit pNFS mount evidence was observed: " + ", ".join(evidence) + ".",
        )

    if protocol_version and protocol_version.split(".", 1)[0] in {"2", "3"}:
        return (
            CapabilityStatus.UNAVAILABLE,
            f"NFSv{protocol_version} does not provide pNFS.",
        )
    return (
        CapabilityStatus.PARTIAL,
        "pNFS is not confirmed: NFSv4 or NFSv4.1 alone is not evidence that a pNFS layout is active.",
    )


def _nfs_details(
    device: str,
    filesystem: str,
    raw_options: str,
    system: str,
    nfsstat: _NFSStatData | None = None,
) -> NFSDetails:
    server, export = _nfs_server_export(device)
    observed_options = _mount_options(
        ",".join([raw_options, *(nfsstat.mount_options if nfsstat else [])])
    )
    # Mount-table options can include Kerberos principals and realms. Apply the
    # same allowlist to both psutil fallback data and nfsstat enrichment before
    # anything reaches the local API or dashboard.
    options = [
        option
        for option in observed_options
        if _SAFE_NFS_PARAMETER.fullmatch(option)
    ]
    if nfsstat is not None:
        server = nfsstat.server or server
        export = nfsstat.export or export
    protocol_version = _nfs_protocol_version(filesystem, options)
    pnfs_status, pnfs_message = _pnfs_capability(system, protocol_version, options)
    return NFSDetails(
        server=server,
        export=export,
        protocol_version=protocol_version,
        mount_options=options,
        status_flags=nfsstat.status_flags if nfsstat else [],
        pnfs_status=pnfs_status,
        pnfs_message=pnfs_message,
    )


_SAFE_NFS_PARAMETER = re.compile(
    r"^(?:(?:vers|nfsvers)=\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?|"
    r"minorversion=\d+|(?:r|w|d|rw)size=\d+|"
    r"sec=[A-Za-z0-9_-]+|proto=(?:tcp|udp)(?:6)?|tcp(?:6)?|udp(?:6)?|"
    r"(?:port|mountport|timeo|retrans|maxgroups|readahead|accesscache|"
    r"acregmin|acregmax|acdirmin|acdirmax|deadtimeout)=\d+|"
    r"ro|rw|async|sync|nodev|noexec|nosuid|noatime|nobrowse|automounted|"
    r"hard|soft|intr|nointr|resvport|noresvport|locks|nolocks|locallocks|"
    r"rdirplus|nordirplus|quota|noquota)$",
    re.IGNORECASE,
)


def _parse_nfsstat_mount(
    value: Mapping[str, Any], mount_point: str
) -> _NFSStatData:
    """Extract only an allowlist; discard filehandles and identity fields."""

    mount: Mapping[str, Any] | None = None
    expected_mount = os.path.normpath(mount_point)
    for candidate in value.values():
        if not isinstance(candidate, Mapping):
            continue
        candidate_mount = candidate.get("Mount Point")
        if (
            isinstance(candidate_mount, str)
            and os.path.normpath(candidate_mount) == expected_mount
        ):
            mount = candidate
            break
    if mount is None:
        raise RuntimeError("nfsstat JSON did not contain mount information")

    current = mount.get("Current mount parameters")
    if not isinstance(current, Mapping):
        raise RuntimeError("nfsstat JSON did not contain current mount parameters")
    raw_parameters = current.get("NFS parameters")
    parameters = (
        [item for item in raw_parameters if isinstance(item, str)]
        if isinstance(raw_parameters, list)
        else []
    )
    safe_options = [
        parameter for parameter in parameters if _SAFE_NFS_PARAMETER.fullmatch(parameter)
    ]

    server: str | None = None
    export: str | None = None
    candidates: list[Mapping[str, Any]] = []
    current_location = current.get("Current location")
    if isinstance(current_location, Mapping):
        candidates.append(current_location)
    locations = current.get("File system locations")
    if isinstance(locations, list):
        candidates.extend(item for item in locations if isinstance(item, Mapping))
    for location in candidates:
        raw_server = location.get("Server")
        raw_export = location.get("Export")
        if isinstance(raw_server, str) and raw_server.strip():
            server = raw_server.strip()
        if isinstance(raw_export, str) and raw_export.strip():
            export = raw_export.strip()
        if server or export:
            break
    status_flags: list[str] = []
    raw_status = mount.get("Status flags")
    if isinstance(raw_status, Mapping):
        raw_flags = raw_status.get("Flags")
        if isinstance(raw_flags, list):
            allowed_statuses = {"dead", "not responding", "recovery"}
            status_flags = sorted(
                {
                    item.casefold()
                    for item in raw_flags
                    if isinstance(item, str) and item.casefold() in allowed_statuses
                }
            )
    return _NFSStatData(
        server=server,
        export=export,
        mount_options=_mount_options(",".join(safe_options)),
        status_flags=status_flags,
    )


def _roles(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value if item]
    return []


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _volume_name(partition: PartitionLike, info: Mapping[str, Any] | None) -> str:
    if info:
        for key in ("VolumeName", "MediaName"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if partition.mountpoint == "/":
        return "Root"
    return Path(partition.mountpoint).name or partition.mountpoint


def _volume_id(device: str, mountpoint: str) -> str:
    material = f"{device}\0{mountpoint}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(material).hexdigest()[:16]


def _health(
    family: FilesystemFamily,
    system: str,
    info: Mapping[str, Any] | None,
    diskutil_error: str | None,
) -> VolumeHealth:
    if family == FilesystemFamily.NFS:
        return VolumeHealth(
            status=CapabilityStatus.UNAVAILABLE,
            message="Backing-device health must be reported by the NFS server.",
        )
    if system != "Darwin":
        return VolumeHealth(
            status=CapabilityStatus.UNAVAILABLE,
            message="Backing-device health collection is currently macOS-only.",
        )
    if diskutil_error:
        return VolumeHealth(
            status=CapabilityStatus.ERROR,
            message=f"diskutil metadata unavailable: {diskutil_error}",
        )
    if info is None:
        return VolumeHealth(
            status=CapabilityStatus.UNAVAILABLE,
            message="No backing-device health source is available for this mount.",
        )

    return smart_health(info.get("SMARTStatus"), source="diskutil")


def smart_health(raw: object, *, source: str) -> VolumeHealth:
    """Map a device-reported SMART string to capability state.

    Shared by the per-mount ``diskutil`` path and the NVMe controller path so
    both label the same words the same way. Missing or unsupported values are
    ``unavailable``, never ``healthy``.
    """

    if isinstance(raw, str) and raw.strip():
        value = raw.strip()
        normalized = value.casefold().replace("_", " ").replace("-", " ")
        if normalized in {
            "unsupported",
            "not supported",
            "unknown",
            "not applicable",
            "n/a",
            "unavailable",
        }:
            return VolumeHealth(
                status=CapabilityStatus.UNAVAILABLE,
                smart_status=value,
                message="This device does not expose a usable SMART result.",
            )
        if normalized in {"verified", "passed", "pass", "ok"}:
            return VolumeHealth(
                status=CapabilityStatus.AVAILABLE,
                smart_status=value,
                message=(
                    "Device-reported SMART status; this is not an independent diagnosis."
                ),
            )
        if any(token in normalized for token in ("fail", "error", "critical")):
            return VolumeHealth(
                status=CapabilityStatus.ERROR,
                smart_status=value,
                message="The device reported a SMART health problem.",
            )
        return VolumeHealth(
            status=CapabilityStatus.PARTIAL,
            smart_status=value,
            message="SMART returned a non-passing value that requires investigation.",
        )
    return VolumeHealth(
        status=CapabilityStatus.UNAVAILABLE,
        message=f"This device did not expose SMART status through {source}.",
    )


class VolumeCollector:
    """Collect mounted filesystems without requiring elevated privileges."""

    def __init__(
        self,
        *,
        partitions_provider: Callable[..., Sequence[PartitionLike]] = psutil.disk_partitions,
        usage_provider: Callable[[str], UsageLike] = psutil.disk_usage,
        system_provider: Callable[[], str] = platform.system,
        diskutil_runner: DiskutilRunner = diskutil_info_plist,
        nfsstat_runner: NFSStatRunner = nfsstat_mount_json,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        diskutil_cache_ttl_seconds: float = DISKUTIL_CACHE_TTL_SECONDS,
        nfsstat_cache_ttl_seconds: float = NFSSTAT_CACHE_TTL_SECONDS,
    ) -> None:
        self._partitions_provider = partitions_provider
        self._usage_provider = usage_provider
        self._system_provider = system_provider
        self._diskutil_runner = diskutil_runner
        self._nfsstat_runner = nfsstat_runner
        self._now = now
        self._monotonic = monotonic
        self._diskutil_cache_ttl_seconds = max(0.0, diskutil_cache_ttl_seconds)
        self._nfsstat_cache_ttl_seconds = max(0.0, nfsstat_cache_ttl_seconds)
        self._diskutil_cache: dict[str, _CachedDiskutil] = {}
        self._nfsstat_cache: dict[str, _CachedNFSStat] = {}
        self._diskutil_lock = threading.Lock()
        self._nfsstat_lock = threading.Lock()

    def _cached_diskutil_info(
        self, target: str
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        """Cache positive and negative metadata to keep one-second polling cheap."""

        with self._diskutil_lock:
            current_time = self._monotonic()
            cached = self._diskutil_cache.get(target)
            if cached is not None and current_time < cached.expires_at:
                return cached.info, cached.error
            try:
                info = self._diskutil_runner(target, DISKUTIL_TIMEOUT_SECONDS)
                error = None
            except (OSError, RuntimeError, TimeoutError) as exc:
                info = None
                error = _clean_error(exc)
            self._diskutil_cache[target] = _CachedDiskutil(
                expires_at=current_time + self._diskutil_cache_ttl_seconds,
                info=info,
                error=error,
            )
            return info, error

    def _cached_nfsstat_info(
        self, mount_point: str
    ) -> tuple[_NFSStatData | None, str | None]:
        with self._nfsstat_lock:
            current_time = self._monotonic()
            cached = self._nfsstat_cache.get(mount_point)
            if cached is not None and current_time < cached.expires_at:
                return cached.info, cached.error
            try:
                raw = self._nfsstat_runner(mount_point, NFSSTAT_TIMEOUT_SECONDS)
                info = _parse_nfsstat_mount(raw, mount_point)
                error = None
            except (OSError, RuntimeError, TimeoutError) as exc:
                info = None
                error = _clean_error(exc)
            self._nfsstat_cache[mount_point] = _CachedNFSStat(
                expires_at=current_time + self._nfsstat_cache_ttl_seconds,
                info=info,
                error=error,
            )
            return info, error

    def collect(self) -> VolumesResponse:
        sampled_at = self._now()
        system = self._system_provider()
        errors: list[str] = []

        try:
            # On macOS, psutil's ``all=False`` requires the mount source to be
            # an existing absolute path.  That silently removes NFS sources
            # such as ``server:/export``, so enumerate all mounts and apply our
            # own explicit pseudo-filesystem filter below.
            partitions = list(self._partitions_provider(all=True))
        except (OSError, psutil.Error) as exc:
            return VolumesResponse(
                sampled_at=sampled_at,
                status=CapabilityStatus.ERROR,
                source="psutil.disk_partitions",
                message=f"Mounted volumes could not be enumerated: {_clean_error(exc)}",
                items=[],
            )

        partitions = [
            partition
            for partition in partitions
            if _is_storage_filesystem(partition.fstype)
        ]
        if not partitions:
            partitions = [_RootFallback()]

        unique_partitions: list[PartitionLike] = []
        seen_mounts: set[str] = set()
        for partition in partitions[:MAX_MOUNTS]:
            mountpoint = os.path.abspath(partition.mountpoint)
            if mountpoint in seen_mounts:
                continue
            seen_mounts.add(mountpoint)
            unique_partitions.append(partition)
        if len(partitions) > MAX_MOUNTS:
            errors.append(f"Only the first {MAX_MOUNTS} mounts were inspected.")

        items: list[Volume] = []
        used_diskutil = False
        used_nfsstat = False
        for partition in unique_partitions:
            try:
                usage = self._usage_provider(partition.mountpoint)
            except (OSError, psutil.Error) as exc:
                errors.append(
                    f"{partition.mountpoint}: capacity unavailable ({_clean_error(exc)})"
                )
                continue

            family = _filesystem_family(partition.fstype)
            info: Mapping[str, Any] | None = None
            diskutil_error: str | None = None
            nfsstat_info: _NFSStatData | None = None
            # diskutil is useful for local Apple volumes. Querying a remote export
            # cannot reveal the server's physical-disk health.
            if system == "Darwin" and not _is_remote(partition.device, partition.fstype):
                used_diskutil = True
                info, diskutil_error = self._cached_diskutil_info(
                    partition.device or partition.mountpoint
                )
                if diskutil_error:
                    errors.append(f"{partition.mountpoint}: {diskutil_error}")
            elif system == "Darwin" and family == FilesystemFamily.NFS:
                used_nfsstat = True
                nfsstat_info, nfsstat_error = self._cached_nfsstat_info(
                    partition.mountpoint
                )
                if nfsstat_error:
                    errors.append(
                        f"{partition.mountpoint}: nfsstat metadata unavailable ({nfsstat_error})"
                    )

            filesystem = partition.fstype.strip().lower() or "unknown"
            if info:
                diskutil_fs = info.get("FilesystemType")
                if isinstance(diskutil_fs, str) and diskutil_fs.strip():
                    filesystem = diskutil_fs.strip().lower()
                    family = _filesystem_family(filesystem)

            apfs: APFSDetails | None = None
            if family == FilesystemFamily.APFS:
                apfs = APFSDetails(
                    container_reference=(
                        str(info["APFSContainerReference"])
                        if info and info.get("APFSContainerReference")
                        else None
                    ),
                    volume_uuid=(
                        str(info["VolumeUUID"])
                        if info and info.get("VolumeUUID")
                        else None
                    ),
                    roles=_roles(info.get("APFSVolumeRole") if info else None),
                    encrypted=_optional_bool(
                        info.get("APFSEncrypted", info.get("Encrypted")) if info else None
                    ),
                )

            opts = {item.casefold() for item in _mount_options(partition.opts)}
            total = max(0, int(usage.total))
            used = max(0, int(usage.used))
            available = max(0, int(usage.free))
            percent = min(100.0, max(0.0, round(float(usage.percent), 2)))
            items.append(
                Volume(
                    id=_volume_id(partition.device, partition.mountpoint),
                    name=_volume_name(partition, info),
                    mount_point=partition.mountpoint,
                    device=partition.device or "unknown",
                    filesystem=filesystem,
                    filesystem_family=family,
                    remote=_is_remote(partition.device, filesystem),
                    read_only="ro" in opts,
                    total_bytes=total,
                    used_bytes=min(used, total) if total else used,
                    available_bytes=min(available, total) if total else available,
                    used_percent=percent,
                    health=_health(family, system, info, diskutil_error),
                    apfs=apfs,
                    nfs=(
                        _nfs_details(
                            partition.device,
                            filesystem,
                            partition.opts,
                            system,
                            nfsstat_info,
                        )
                        if family == FilesystemFamily.NFS
                        else None
                    ),
                )
            )

        source = "psutil.disk_partitions+disk_usage"
        if used_diskutil:
            source += "+diskutil_plist"
        if used_nfsstat:
            source += "+nfsstat_json"
        if not items:
            return VolumesResponse(
                sampled_at=sampled_at,
                status=CapabilityStatus.ERROR,
                source=source,
                message="No mounted volume had readable capacity information."
                + (f" {'; '.join(errors[:3])}" if errors else ""),
                items=[],
            )

        status = CapabilityStatus.PARTIAL if errors else CapabilityStatus.AVAILABLE
        return VolumesResponse(
            sampled_at=sampled_at,
            status=status,
            source=source,
            message="; ".join(errors[:3]) if errors else None,
            items=sorted(items, key=lambda item: item.mount_point),
        )
