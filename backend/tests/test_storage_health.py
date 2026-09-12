import plistlib
import subprocess
from datetime import datetime, timezone

from localtrace_backend.collectors.storage_health import (
    StorageHealthCollector,
    parse_apfs_list,
    parse_local_snapshots,
    parse_nvme_profile,
)
from localtrace_backend.models import CapabilityStatus

NOW = datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc)

APFS_LIST = plistlib.dumps(
    {
        "Containers": [
            {
                "APFSContainerUUID": "AAAA-1111",
                "ContainerReference": "disk3",
                "CapacityCeiling": 1_000_000,
                "CapacityFree": 250_000,
                "Fusion": False,
                "PhysicalStores": [{"DeviceIdentifier": "disk0s2", "Size": 1_000_000}],
                "Volumes": [
                    {
                        "DeviceIdentifier": "disk3s1",
                        "Name": "Macintosh HD",
                        "Roles": ["System"],
                        "CapacityInUse": 10_000,
                        "FileVault": True,
                        "Locked": False,
                        "Sealed": "Yes",
                        "Encryption": True,
                    },
                    {
                        "DeviceIdentifier": "disk3s5",
                        "Name": "Macintosh HD - Data",
                        "Roles": ["Data"],
                        "CapacityInUse": 700_000,
                        "CapacityQuota": 900_000,
                        "CapacityReserve": 50_000,
                        "FileVault": True,
                        "Locked": False,
                        "Sealed": "No",
                    },
                ],
            }
        ]
    }
)

TMUTIL_OUTPUT = """Snapshots for volume group containing disk3s1s1:
com.apple.TimeMachine.2026-09-10-083000.local
com.apple.TimeMachine.2026-09-12-120000.local
com.apple.TimeMachine.2026-09-11-233000.local
"""

NVME_JSON = b"""{
  "SPNVMeDataType": [
    {
      "_name": "NVMExpress",
      "_items": [
        {
          "_name": "APPLE SSD AP1024Z",
          "bsd_name": "disk0",
          "device_model": "APPLE SSD AP1024Z",
          "device_serial": "SECRET-SERIAL",
          "size": "1 TB",
          "size_in_bytes": 1000000000000,
          "spnvme_smart_status": "Verified",
          "spnvme_trim_support": "Yes",
          "removable_media": "no",
          "spnvme_linkwidth": "x4",
          "spnvme_linkspeed": "8.0 GT/s",
          "_items": [
            {"_name": "disk0s1", "bsd_name": "disk0s1", "size_in_bytes": 524288000}
          ]
        }
      ]
    }
  ]
}"""


class FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def make_runner(responses: dict[str, object]):
    calls: list[list[str]] = []

    def runner(argv, timeout):
        calls.append(list(argv))
        key = argv[0].rsplit("/", 1)[-1]
        response = responses.get(key)
        if isinstance(response, Exception):
            raise response
        if response is None:
            return FakeProcess(b"", returncode=1, stderr=b"missing fixture")
        return response

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def make_collector(responses, system: str = "Darwin", mounts=None) -> StorageHealthCollector:
    return StorageHealthCollector(
        mount_points_provider=lambda: mounts or ["/"],
        system_provider=lambda: system,
        runner=make_runner(responses),
        now=lambda: NOW,
    )


HAPPY = {
    "diskutil": FakeProcess(APFS_LIST),
    "tmutil": FakeProcess(TMUTIL_OUTPUT.encode()),
    "system_profiler": FakeProcess(NVME_JSON),
}


# -- parsers ----------------------------------------------------------------


def test_parse_apfs_list_keeps_allowlisted_fields_and_native_quota() -> None:
    containers = parse_apfs_list(APFS_LIST)

    assert len(containers) == 1
    container = containers[0]
    assert container.reference == "disk3"
    assert container.uuid == "AAAA-1111"
    assert container.capacity_ceiling_bytes == 1_000_000
    assert container.capacity_free_bytes == 250_000
    assert container.used_percent == 75.0
    assert container.physical_stores == ["disk0s2"]
    assert container.fusion is False
    data = next(v for v in container.volumes if v.device == "disk3s5")
    assert data.capacity_quota_bytes == 900_000
    assert data.capacity_reserve_bytes == 50_000
    assert data.filevault is True
    assert data.sealed == "No"
    system = next(v for v in container.volumes if v.device == "disk3s1")
    assert system.capacity_quota_bytes is None
    assert system.sealed == "Yes"


def test_parse_local_snapshots_counts_and_dates_without_sizes() -> None:
    group = parse_local_snapshots("/", TMUTIL_OUTPUT)

    assert group.count == 3
    assert group.volume_group == "disk3s1s1"
    assert group.oldest == datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc)
    assert group.newest == datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    assert len(group.recent_names) == 3
    assert not hasattr(group, "size_bytes")


def test_parse_local_snapshots_handles_no_snapshots() -> None:
    group = parse_local_snapshots("/", "Snapshots for volume group containing disk3s1s1:\n")

    assert group.count == 0
    assert group.oldest is None


def test_parse_nvme_profile_drops_serial_and_partitions() -> None:
    devices = parse_nvme_profile(NVME_JSON)

    assert len(devices) == 1
    device = devices[0]
    assert device.name == "APPLE SSD AP1024Z"
    assert device.bsd_name == "disk0"
    assert device.size_bytes == 1_000_000_000_000
    assert device.health.status == CapabilityStatus.AVAILABLE
    assert device.health.smart_status == "Verified"
    assert device.trim_support is True
    assert device.removable is False
    assert device.link_width == "x4"
    dumped = device.model_dump_json()
    assert "SECRET" not in dumped
    assert "disk0s1" not in dumped


def test_parse_nvme_unsupported_smart_is_unavailable_not_healthy() -> None:
    payload = NVME_JSON.replace(b'"Verified"', b'"Not Supported"')

    devices = parse_nvme_profile(payload)

    assert devices[0].health.status == CapabilityStatus.UNAVAILABLE


# -- collector --------------------------------------------------------------


def test_refresh_now_runs_all_probes_with_absolute_paths_and_timeouts() -> None:
    collector = make_collector(HAPPY)

    result = collector.refresh_now()

    calls = collector._runner.calls  # type: ignore[attr-defined]
    assert [call[0] for call in calls] == [
        "/usr/sbin/diskutil",
        "/usr/bin/tmutil",
        "/usr/sbin/system_profiler",
    ]
    assert calls[0][1:] == ["apfs", "list", "-plist"]
    assert calls[1][1:] == ["listlocalsnapshots", "/"]
    assert calls[2][1:] == ["SPNVMeDataType", "-json"]
    assert result.status == CapabilityStatus.AVAILABLE
    assert result.refreshed_at == NOW
    assert result.apfs.status == CapabilityStatus.AVAILABLE
    assert "1 volume(s) carry a native APFS capacity quota" in (result.apfs.message or "")
    assert result.snapshots.items[0].count == 3
    assert result.nvme.items[0].health.smart_status == "Verified"


def test_one_failing_probe_degrades_to_partial_only() -> None:
    responses = dict(HAPPY)
    responses["system_profiler"] = subprocess.TimeoutExpired(cmd="system_profiler", timeout=15)
    collector = make_collector(responses)

    result = collector.refresh_now()

    assert result.status == CapabilityStatus.PARTIAL
    assert result.nvme.status == CapabilityStatus.ERROR
    assert "did not respond" in (result.nvme.message or "")
    assert result.apfs.status == CapabilityStatus.AVAILABLE
    assert "NVMe SMART" in (result.message or "")


def test_broken_seal_is_flagged() -> None:
    responses = dict(HAPPY)
    responses["diskutil"] = FakeProcess(APFS_LIST.replace(b"<string>Yes</string>", b"<string>Broken</string>"))
    collector = make_collector(responses)

    result = collector.refresh_now()

    assert result.apfs.status == CapabilityStatus.PARTIAL
    assert "disk3s1" in (result.apfs.message or "")


def test_snapshot_probe_dedupes_mounts_in_the_same_volume_group() -> None:
    collector = make_collector(HAPPY, mounts=["/", "/System/Volumes/Data"])

    result = collector.refresh_now()

    assert len(result.snapshots.items) == 1
    calls = [call for call in collector._runner.calls if call[0].endswith("tmutil")]  # type: ignore[attr-defined]
    assert len(calls) == 2


def test_non_macos_is_unavailable_and_runs_nothing() -> None:
    collector = make_collector(HAPPY, system="Linux")

    result = collector.refresh_now()

    assert result.status == CapabilityStatus.UNAVAILABLE
    assert collector._runner.calls == []  # type: ignore[attr-defined]


def test_snapshot_is_warming_up_before_first_refresh() -> None:
    collector = make_collector(HAPPY)

    assert collector.snapshot().status == CapabilityStatus.WARMING_UP
    assert collector.snapshot().refreshed_at is None


def test_background_loop_publishes_and_stops() -> None:
    import time

    collector = StorageHealthCollector(
        system_provider=lambda: "Darwin",
        runner=make_runner(HAPPY),
        refresh_interval_seconds=0.05,
    )
    collector.start()
    try:
        for _ in range(200):
            if collector.snapshot().status != CapabilityStatus.WARMING_UP:
                break
            time.sleep(0.01)
    finally:
        collector.stop()

    assert collector.snapshot().status == CapabilityStatus.AVAILABLE
