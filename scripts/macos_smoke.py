#!/usr/bin/env python3
"""Exercise LocalTrace's real macOS volume, quota, I/O, usage, and health collector paths."""

from __future__ import annotations

from pathlib import Path
import os
import platform
import tempfile
import time

import psutil

from localtrace_backend.collectors.fs_benchmark import FilesystemBenchmarkService
from localtrace_backend.collectors.io import DiskIOSampler
from localtrace_backend.collectors.storage_health import StorageHealthCollector
from localtrace_backend.collectors.usage import DiskUsageScanner
from localtrace_backend.collectors.quotas import (
    QuotaCollector,
    reconcile_quota_semantics,
)
from localtrace_backend.collectors.volumes import (
    VolumeCollector,
    diskutil_info_plist,
)
from localtrace_backend.models import CapabilityStatus, FilesystemFamily


def main() -> int:
    if platform.system() != "Darwin":
        raise RuntimeError("this smoke check must run on macOS")

    diskutil = Path("/usr/sbin/diskutil")
    if not diskutil.is_file():
        raise RuntimeError(f"expected macOS diskutil at {diskutil}")
    quota = Path("/usr/bin/quota")
    if not quota.is_file():
        raise RuntimeError(f"expected macOS quota utility at {quota}")

    # This directly exercises the collector's absolute, structured diskutil
    # path. SMART and NFS are intentionally not required on a hosted runner.
    root_metadata = diskutil_info_plist("/")
    if not root_metadata:
        raise RuntimeError("diskutil returned no structured metadata for /")

    volume_collector = VolumeCollector()
    quota_collector = QuotaCollector()
    io_sampler = DiskIOSampler()
    first_volumes = volume_collector.collect()
    quotas = quota_collector.collect()
    first_io = io_sampler.sample()
    time.sleep(0.25)
    second_volumes = volume_collector.collect()
    second_io = io_sampler.sample()

    valid_volume_states = {CapabilityStatus.AVAILABLE, CapabilityStatus.PARTIAL}
    if first_volumes.status not in valid_volume_states or not first_volumes.items:
        raise RuntimeError(
            "first real volume collection failed: "
            f"{first_volumes.status.value} {first_volumes.message or ''}".strip()
        )
    if second_volumes.status not in valid_volume_states or not second_volumes.items:
        raise RuntimeError(
            "second real volume collection failed: "
            f"{second_volumes.status.value} {second_volumes.message or ''}".strip()
        )
    # Hosted runners normally have no NFS mount. If a future runner or local
    # invocation does, verify that complete mount-table discovery preserves it.
    native_nfs_mounts = {
        partition.mountpoint
        for partition in psutil.disk_partitions(all=True)
        if partition.fstype.casefold() in {"nfs", "nfs4"}
    }
    observed_nfs_mounts = {
        volume.mount_point
        for volume in second_volumes.items
        if volume.filesystem_family == FilesystemFamily.NFS
    }
    missing_nfs_mounts = native_nfs_mounts - observed_nfs_mounts
    if missing_nfs_mounts:
        raise RuntimeError(
            "real NFS mounts were lost during collection: "
            + ", ".join(sorted(missing_nfs_mounts))
        )
    # A hosted macOS runner does not need configured quota limits. The probe
    # must still execute and classify that absence without fabricating zeros.
    if quotas.status not in {
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.PARTIAL,
    }:
        raise RuntimeError(
            "real current-user quota collection failed: "
            f"{quotas.status.value} {quotas.message or ''}".strip()
        )
    # Exercise the same exact mount/version reconciliation used by the
    # dashboard without requiring a quota-configured hosted runner.
    reconciled_quotas = reconcile_quota_semantics(quotas, second_volumes)
    if len(reconciled_quotas.items) != len(quotas.items):
        raise RuntimeError("quota reconciliation changed the number of rows")
    if first_io.status != CapabilityStatus.WARMING_UP:
        raise RuntimeError(
            f"first I/O sample should warm up, got {first_io.status.value}"
        )
    if second_io.status not in {
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.PARTIAL,
    }:
        raise RuntimeError(
            "second real I/O sample failed: "
            f"{second_io.status.value} {second_io.message or ''}".strip()
        )
    if second_io.interval_seconds < 0.05:
        raise RuntimeError(
            f"I/O sample interval was too short: {second_io.interval_seconds}"
        )

    # The usage scanner must group a real file by its real owner on APFS and
    # report st_blocks-derived allocation without following symlinks.
    with tempfile.TemporaryDirectory(prefix="localtrace-smoke-") as scratch:
        sample = Path(scratch) / "sample.gguf"
        sample.write_bytes(b"L" * 65_536)
        (Path(scratch) / "link.gguf").symlink_to(sample)
        usage = DiskUsageScanner(directories_provider=lambda: [scratch]).scan_now()
        if usage.status != CapabilityStatus.AVAILABLE:
            raise RuntimeError(
                f"usage scan failed: {usage.status.value} {usage.message or ''}".strip()
            )
        if usage.file_count != 1 or usage.total_apparent_bytes != 65_536:
            raise RuntimeError(
                "usage scan must count the regular file once and skip the symlink: "
                f"{usage.file_count} file(s), {usage.total_apparent_bytes} bytes"
            )
        if not usage.owners or usage.owners[0].uid != os.getuid():
            raise RuntimeError("usage scan did not attribute the file to the current uid")

    # A small throughput run must succeed on the temp volume with the macOS
    # cache-bypass and stable-flush controls actually engaged.
    from localtrace_backend.models import BenchmarkRequest, BenchmarkStatus

    bench = FilesystemBenchmarkService(volumes_provider=lambda: volumes)
    job = bench.start(BenchmarkRequest(mount_point="/", size_bytes=8 * 1024 * 1024))
    bench.wait(timeout=60)
    job = bench.get(job.id)
    if job is None or job.status != BenchmarkStatus.COMPLETED:
        raise RuntimeError(f"throughput run failed: {job.message if job else 'missing job'}")
    if not job.cache_bypass or job.flush_method != "F_FULLFSYNC":
        raise RuntimeError(
            f"macOS controls not engaged: cache_bypass={job.cache_bypass} flush={job.flush_method}"
        )
    if job.write is None or job.read is None or job.write.bytes_per_second <= 0:
        raise RuntimeError("throughput run produced no rates")
    if job.mount_point is None:
        raise RuntimeError("throughput run was not attributed to a mount")

    # Storage-health probes must execute the real utilities. A hosted runner
    # may legitimately have no NVMe controller or no local snapshots; the
    # probes must classify that honestly rather than fail or fabricate.
    storage_health = StorageHealthCollector().refresh_now()
    if storage_health.status == CapabilityStatus.ERROR:
        raise RuntimeError(
            "every storage-health probe failed: "
            f"apfs={storage_health.apfs.message} "
            f"snapshots={storage_health.snapshots.message} "
            f"nvme={storage_health.nvme.message}"
        )
    if storage_health.apfs.status not in {
        CapabilityStatus.AVAILABLE,
        CapabilityStatus.PARTIAL,
    }:
        raise RuntimeError(
            f"diskutil apfs list did not produce containers: {storage_health.apfs.message}"
        )
    if storage_health.snapshots.status == CapabilityStatus.ERROR:
        raise RuntimeError(f"tmutil probe failed: {storage_health.snapshots.message}")

    print(
        "macOS collector smoke passed: "
        f"{len(second_volumes.items)} volume(s), "
        f"quota={reconciled_quotas.status.value} "
        f"({len(reconciled_quotas.items)} configured), "
        f"{len(second_io.devices)} whole-device counter(s), "
        f"{second_io.interval_seconds:.3f}s interval, "
        f"usage owner={usage.owners[0].owner_name or usage.owners[0].uid}, "
        f"apfs containers={len(storage_health.apfs.items)}, "
        f"snapshots={storage_health.snapshots.status.value}, "
        f"nvme={storage_health.nvme.status.value}, "
        f"throughput on {job.mount_point}: write {job.write.gb_per_second} GB/s "
        f"read {job.read.gb_per_second} GB/s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
