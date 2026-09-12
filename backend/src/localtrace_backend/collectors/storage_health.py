"""Filesystem and backing-device health signals beyond per-mount capacity.

Three independent probes, each with its own capability state, refreshed by a
background thread so the dashboard never waits on a slow system utility:

- ``diskutil apfs list -plist``: container ceiling/free space, physical
  stores, and per-volume roles, FileVault, lock state, seal state, and native
  APFS volume quotas/reserves.
- ``tmutil listlocalsnapshots <mount>``: count and age of local Time Machine
  snapshots, which silently hold space on the container.
- ``system_profiler SPNVMeDataType -json``: NVMe controller model, size, TRIM
  support, and device-reported SMART status, a second source when
  ``diskutil info`` says SMART is not supported for a mount.

Accuracy rules:

- All three utilities are invoked with absolute paths, argument arrays, and
  timeouts. Only allowlisted fields are retained; serial numbers are dropped.
- ``tmutil`` does not report snapshot size, so none is invented.
- A SMART value is a device self-report and is labeled as such. Missing or
  unsupported values are ``unavailable``, never healthy.
- A probe that fails leaves the other two intact; the aggregate is
  ``partial`` unless every probe failed.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from localtrace_backend.collectors.volumes import smart_health
from localtrace_backend.models import (
    APFSContainer,
    APFSContainersProbe,
    APFSContainerVolume,
    CapabilityStatus,
    NVMeDevice,
    NVMeProbe,
    SnapshotGroup,
    SnapshotsProbe,
    StorageHealthResponse,
)

DISKUTIL = "/usr/sbin/diskutil"
TMUTIL = "/usr/bin/tmutil"
SYSTEM_PROFILER = "/usr/sbin/system_profiler"
DISKUTIL_TIMEOUT_SECONDS = 4.0
TMUTIL_TIMEOUT_SECONDS = 4.0
SYSTEM_PROFILER_TIMEOUT_SECONDS = 15.0
DEFAULT_REFRESH_INTERVAL_SECONDS = 60.0
MAX_RECENT_SNAPSHOT_NAMES = 5
SOURCE = "diskutil apfs list+tmutil listlocalsnapshots+system_profiler SPNVMeDataType"

Runner = Callable[[Sequence[str], float], Any]
_SNAPSHOT_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: object, limit: int = 200) -> str:
    return " ".join(str(value).split())[:limit]


def _run(argv: Sequence[str], timeout: float) -> Any:
    return subprocess.run(  # noqa: S603 - absolute path, argument array, no shell
        list(argv), capture_output=True, timeout=timeout, check=False
    )


def _completed(runner: Runner, argv: Sequence[str], timeout: float) -> bytes:
    """Run a utility and return stdout bytes, raising a clean error otherwise."""

    try:
        result = runner(argv, timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"{os.path.basename(argv[0])} did not respond within {timeout:g}s") from exc
    returncode = getattr(result, "returncode", 0)
    stdout = getattr(result, "stdout", b"")
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    if returncode != 0:
        stderr = getattr(result, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            _clean(stderr) or f"{os.path.basename(argv[0])} exited with {returncode}"
        )
    return stdout


def _nonneg_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"yes", "true", "on", "1"}:
            return True
        if lowered in {"no", "false", "off", "0"}:
            return False
    return None


def _optional_str(value: object, limit: int = 120) -> str | None:
    if isinstance(value, str) and value.strip():
        return _clean(value, limit)
    return None


# -- APFS containers --------------------------------------------------------


def parse_apfs_list(payload: bytes) -> list[APFSContainer]:
    """Parse ``diskutil apfs list -plist`` into allowlisted container rows."""

    root = plistlib.loads(payload)
    if not isinstance(root, dict):
        raise RuntimeError("diskutil apfs list returned an unexpected root")
    containers = root.get("Containers")
    if not isinstance(containers, list):
        return []
    rows: list[APFSContainer] = []
    for entry in containers:
        if not isinstance(entry, Mapping):
            continue
        reference = _optional_str(entry.get("ContainerReference"))
        if reference is None:
            continue
        ceiling = _nonneg_int(entry.get("CapacityCeiling")) or 0
        free = _nonneg_int(entry.get("CapacityFree")) or 0
        used_percent = (
            round(max(0.0, min(100.0, (ceiling - free) / ceiling * 100)), 2)
            if ceiling > 0
            else 0.0
        )
        stores = entry.get("PhysicalStores")
        physical_stores = [
            device
            for store in (stores if isinstance(stores, list) else [])
            if isinstance(store, Mapping)
            for device in [_optional_str(store.get("DeviceIdentifier"))]
            if device
        ]
        volumes: list[APFSContainerVolume] = []
        for volume in entry.get("Volumes") or []:
            if not isinstance(volume, Mapping):
                continue
            device = _optional_str(volume.get("DeviceIdentifier"))
            if device is None:
                continue
            roles_raw = volume.get("Roles")
            roles = [
                _clean(role, 40)
                for role in (roles_raw if isinstance(roles_raw, list) else [])
                if isinstance(role, str) and role.strip()
            ]
            volumes.append(
                APFSContainerVolume(
                    device=device,
                    name=_optional_str(volume.get("Name")),
                    roles=roles,
                    capacity_in_use_bytes=_nonneg_int(volume.get("CapacityInUse")),
                    capacity_quota_bytes=_nonneg_int(volume.get("CapacityQuota")),
                    capacity_reserve_bytes=_nonneg_int(volume.get("CapacityReserve")),
                    filevault=_optional_bool(volume.get("FileVault")),
                    locked=_optional_bool(volume.get("Locked")),
                    sealed=_optional_str(volume.get("Sealed"), 20),
                )
            )
        rows.append(
            APFSContainer(
                reference=reference,
                uuid=_optional_str(entry.get("APFSContainerUUID")),
                capacity_ceiling_bytes=ceiling,
                capacity_free_bytes=free,
                used_percent=used_percent,
                fusion=_optional_bool(entry.get("Fusion")),
                physical_stores=physical_stores,
                volumes=volumes,
            )
        )
    return rows


# -- Local snapshots --------------------------------------------------------


def parse_local_snapshots(mount_point: str, text: str) -> SnapshotGroup:
    """Parse ``tmutil listlocalsnapshots`` output for one mount point."""

    volume_group: str | None = None
    names: list[str] = []
    stamps: list[datetime] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("snapshots for"):
            match = re.search(r"(disk\d+s\d+(?:s\d+)?)", line)
            volume_group = match.group(1) if match else _clean(line, 80)
            continue
        names.append(_clean(line, 120))
        match = _SNAPSHOT_STAMP.search(line)
        if match:
            year, month, day, hour, minute, second = (int(part) for part in match.groups())
            try:
                stamps.append(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc))
            except ValueError:
                continue
    return SnapshotGroup(
        mount_point=mount_point,
        volume_group=volume_group,
        count=len(names),
        oldest=min(stamps) if stamps else None,
        newest=max(stamps) if stamps else None,
        recent_names=names[-MAX_RECENT_SNAPSHOT_NAMES:],
    )


# -- NVMe controllers -------------------------------------------------------


def parse_nvme_profile(payload: bytes) -> list[NVMeDevice]:
    """Parse ``system_profiler SPNVMeDataType -json`` into device rows.

    Serial numbers and any field not listed here are discarded.
    """

    root = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(root, dict):
        raise RuntimeError("system_profiler returned an unexpected root")
    controllers = root.get("SPNVMeDataType")
    if not isinstance(controllers, list):
        return []
    rows: list[NVMeDevice] = []
    stack: list[Any] = list(controllers)
    while stack:
        node = stack.pop(0)
        if not isinstance(node, Mapping):
            continue
        children = node.get("_items")
        if isinstance(children, list):
            stack.extend(children)
        # Only nodes that carry a SMART field are devices; the top-level
        # controller bus node and partition children are not.
        if "spnvme_smart_status" not in node and "device_model" not in node:
            continue
        name = _optional_str(node.get("_name")) or "NVMe device"
        rows.append(
            NVMeDevice(
                name=name,
                bsd_name=_optional_str(node.get("bsd_name")),
                model=_optional_str(node.get("device_model")),
                size_bytes=_nonneg_int(node.get("size_in_bytes")),
                health=smart_health(node.get("spnvme_smart_status"), source="system_profiler"),
                trim_support=_optional_bool(node.get("spnvme_trim_support")),
                removable=_optional_bool(node.get("removable_media")),
                link_speed=_optional_str(node.get("spnvme_linkspeed"), 40),
                link_width=_optional_str(node.get("spnvme_linkwidth"), 40),
            )
        )
    return rows


# -- Collector --------------------------------------------------------------


class StorageHealthCollector:
    """Refresh the three probes in the background and serve the newest result."""

    def __init__(
        self,
        *,
        mount_points_provider: Callable[[], Sequence[str]] = lambda: ["/"],
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        system_provider: Callable[[], str] = platform.system,
        runner: Runner = _run,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        self._mount_points_provider = mount_points_provider
        self.refresh_interval_seconds = refresh_interval_seconds
        self._system = system_provider()
        self._runner = runner
        self._now = now
        self._lock = threading.Lock()
        self._latest: StorageHealthResponse | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_environment(
        cls, mount_points_provider: Callable[[], Sequence[str]] | None = None
    ) -> "StorageHealthCollector":
        raw = os.getenv("LOCALTRACE_STORAGE_HEALTH_INTERVAL_SECONDS")
        interval = DEFAULT_REFRESH_INTERVAL_SECONDS
        if raw:
            try:
                parsed = float(raw)
                if parsed > 0:
                    interval = parsed
            except ValueError:
                pass
        kwargs: dict[str, Any] = {"refresh_interval_seconds": interval}
        if mount_points_provider is not None:
            kwargs["mount_points_provider"] = mount_points_provider
        return cls(**kwargs)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._loop, name="localtrace-storage-health", daemon=True
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
                self.refresh_now()
            except Exception as exc:  # a probe bug must not kill the loop
                with self._lock:
                    self._latest = self._build(
                        refreshed_at=self._now(),
                        apfs=APFSContainersProbe(
                            status=CapabilityStatus.ERROR, source=DISKUTIL, message=_clean(exc)
                        ),
                        snapshots=SnapshotsProbe(
                            status=CapabilityStatus.ERROR, source=TMUTIL, message=_clean(exc)
                        ),
                        nvme=NVMeProbe(
                            status=CapabilityStatus.ERROR,
                            source=SYSTEM_PROFILER,
                            message=_clean(exc),
                        ),
                    )
            self._stop.wait(self.refresh_interval_seconds)

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> StorageHealthResponse:
        with self._lock:
            latest = self._latest
        if latest is not None:
            return latest
        pending = "The first storage-health refresh has not completed yet."
        return self._build(
            refreshed_at=None,
            apfs=APFSContainersProbe(
                status=CapabilityStatus.WARMING_UP, source=DISKUTIL, message=pending
            ),
            snapshots=SnapshotsProbe(
                status=CapabilityStatus.WARMING_UP, source=TMUTIL, message=pending
            ),
            nvme=NVMeProbe(
                status=CapabilityStatus.WARMING_UP, source=SYSTEM_PROFILER, message=pending
            ),
        )

    def _build(
        self,
        *,
        refreshed_at: datetime | None,
        apfs: APFSContainersProbe,
        snapshots: SnapshotsProbe,
        nvme: NVMeProbe,
    ) -> StorageHealthResponse:
        statuses = [apfs.status, snapshots.status, nvme.status]
        if all(status == CapabilityStatus.WARMING_UP for status in statuses):
            overall = CapabilityStatus.WARMING_UP
            message = "Waiting for the first background refresh."
        elif all(status == CapabilityStatus.ERROR for status in statuses):
            overall = CapabilityStatus.ERROR
            message = "Every storage-health probe failed; see each probe's message."
        elif all(status == CapabilityStatus.UNAVAILABLE for status in statuses):
            overall = CapabilityStatus.UNAVAILABLE
            message = "Storage-health probes are macOS-only."
        elif all(status == CapabilityStatus.AVAILABLE for status in statuses):
            overall = CapabilityStatus.AVAILABLE
            message = (
                "SMART values are device self-reports. Snapshot sizes are not "
                "reported by tmutil and are not estimated."
            )
        else:
            overall = CapabilityStatus.PARTIAL
            degraded = [
                name
                for name, status in (
                    ("APFS containers", apfs.status),
                    ("local snapshots", snapshots.status),
                    ("NVMe SMART", nvme.status),
                )
                if status != CapabilityStatus.AVAILABLE
            ]
            message = "Not every probe is available: " + ", ".join(degraded) + "."
        return StorageHealthResponse(
            sampled_at=self._now(),
            status=overall,
            source=SOURCE,
            message=message,
            refreshed_at=refreshed_at,
            refresh_interval_seconds=self.refresh_interval_seconds,
            apfs=apfs,
            snapshots=snapshots,
            nvme=nvme,
        )

    # -- probes ------------------------------------------------------------

    def refresh_now(self) -> StorageHealthResponse:
        """Run all three probes synchronously and publish the result."""

        if self._system != "Darwin":
            reason = "This probe is macOS-only."
            result = self._build(
                refreshed_at=self._now(),
                apfs=APFSContainersProbe(
                    status=CapabilityStatus.UNAVAILABLE, source=DISKUTIL, message=reason
                ),
                snapshots=SnapshotsProbe(
                    status=CapabilityStatus.UNAVAILABLE, source=TMUTIL, message=reason
                ),
                nvme=NVMeProbe(
                    status=CapabilityStatus.UNAVAILABLE, source=SYSTEM_PROFILER, message=reason
                ),
            )
            with self._lock:
                self._latest = result
            return result

        result = self._build(
            refreshed_at=self._now(),
            apfs=self._probe_apfs(),
            snapshots=self._probe_snapshots(),
            nvme=self._probe_nvme(),
        )
        with self._lock:
            self._latest = result
        return result

    def _probe_apfs(self) -> APFSContainersProbe:
        try:
            payload = _completed(
                self._runner, [DISKUTIL, "apfs", "list", "-plist"], DISKUTIL_TIMEOUT_SECONDS
            )
            items = parse_apfs_list(payload)
        except (RuntimeError, TimeoutError, plistlib.InvalidFileException, ValueError) as exc:
            return APFSContainersProbe(
                status=CapabilityStatus.ERROR,
                source=DISKUTIL,
                message=f"diskutil apfs list failed: {_clean(exc)}",
            )
        if not items:
            return APFSContainersProbe(
                status=CapabilityStatus.UNAVAILABLE,
                source=DISKUTIL,
                message="diskutil reported no APFS containers.",
            )
        broken = [
            f"{volume.device}"
            for container in items
            for volume in container.volumes
            if volume.sealed and volume.sealed.casefold() == "broken"
        ]
        quotas = sum(
            1
            for container in items
            for volume in container.volumes
            if volume.capacity_quota_bytes
        )
        if broken:
            return APFSContainersProbe(
                status=CapabilityStatus.PARTIAL,
                source=DISKUTIL,
                message="A sealed volume reports a broken seal: " + ", ".join(broken) + ".",
                items=items,
            )
        return APFSContainersProbe(
            status=CapabilityStatus.AVAILABLE,
            source=DISKUTIL,
            message=(
                f"{len(items)} container(s); {quotas} volume(s) carry a native APFS "
                "capacity quota. Container free space is shared by all member volumes."
            ),
            items=items,
        )

    def _probe_snapshots(self) -> SnapshotsProbe:
        mount_points = [
            os.path.abspath(path) for path in self._mount_points_provider() if path
        ]
        if not mount_points:
            mount_points = ["/"]
        groups: list[SnapshotGroup] = []
        errors: list[str] = []
        seen_groups: set[str] = set()
        for mount_point in dict.fromkeys(mount_points):
            try:
                payload = _completed(
                    self._runner,
                    [TMUTIL, "listlocalsnapshots", mount_point],
                    TMUTIL_TIMEOUT_SECONDS,
                )
            except (RuntimeError, TimeoutError) as exc:
                errors.append(f"{mount_point}: {_clean(exc, 120)}")
                continue
            group = parse_local_snapshots(mount_point, payload.decode("utf-8", errors="replace"))
            key = group.volume_group or mount_point
            if key in seen_groups:
                continue
            seen_groups.add(key)
            groups.append(group)
        if not groups and errors:
            return SnapshotsProbe(
                status=CapabilityStatus.ERROR,
                source=TMUTIL,
                message="tmutil listlocalsnapshots failed: " + "; ".join(errors),
            )
        total = sum(group.count for group in groups)
        message = (
            f"{total} local snapshot(s) across {len(groups)} volume group(s). tmutil does "
            "not report snapshot size, so none is shown."
        )
        if errors:
            return SnapshotsProbe(
                status=CapabilityStatus.PARTIAL,
                source=TMUTIL,
                message=message + " Some mounts failed: " + "; ".join(errors),
                items=groups,
            )
        return SnapshotsProbe(
            status=CapabilityStatus.AVAILABLE, source=TMUTIL, message=message, items=groups
        )

    def _probe_nvme(self) -> NVMeProbe:
        try:
            payload = _completed(
                self._runner,
                [SYSTEM_PROFILER, "SPNVMeDataType", "-json"],
                SYSTEM_PROFILER_TIMEOUT_SECONDS,
            )
            items = parse_nvme_profile(payload)
        except (RuntimeError, TimeoutError, ValueError) as exc:
            return NVMeProbe(
                status=CapabilityStatus.ERROR,
                source=SYSTEM_PROFILER,
                message=f"system_profiler SPNVMeDataType failed: {_clean(exc)}",
            )
        if not items:
            return NVMeProbe(
                status=CapabilityStatus.UNAVAILABLE,
                source=SYSTEM_PROFILER,
                message="No NVMe controller was reported; USB and SATA devices are not covered.",
            )
        failing = [device.name for device in items if device.health.status == CapabilityStatus.ERROR]
        if failing:
            return NVMeProbe(
                status=CapabilityStatus.ERROR,
                source=SYSTEM_PROFILER,
                message="A device reported a SMART problem: " + ", ".join(failing) + ".",
                items=items,
            )
        unverified = [
            device.name
            for device in items
            if device.health.status != CapabilityStatus.AVAILABLE
        ]
        if unverified:
            return NVMeProbe(
                status=CapabilityStatus.PARTIAL,
                source=SYSTEM_PROFILER,
                message="SMART not verified for: " + ", ".join(unverified) + ".",
                items=items,
            )
        return NVMeProbe(
            status=CapabilityStatus.AVAILABLE,
            source=SYSTEM_PROFILER,
            message=f"{len(items)} NVMe device(s) report a passing SMART self-check.",
            items=items,
        )
