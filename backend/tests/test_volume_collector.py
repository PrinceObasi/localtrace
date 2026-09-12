from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from localtrace_backend.collectors.volumes import VolumeCollector
from localtrace_backend.models import CapabilityStatus, FilesystemFamily


NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def test_requests_all_mounts_so_macos_nfs_sources_are_not_filtered() -> None:
    nfs = SimpleNamespace(
        device="server.example:/models",
        mountpoint="/Volumes/models",
        fstype="nfs",
        opts="rw,vers=4.1",
    )
    requested: list[bool] = []

    def partitions_provider(*, all: bool):
        requested.append(all)
        return [nfs]

    collector = VolumeCollector(
        partitions_provider=partitions_provider,
        usage_provider=lambda _: SimpleNamespace(
            total=100, used=10, free=90, percent=10
        ),
        system_provider=lambda: "Linux",
        now=lambda: NOW,
    )

    result = collector.collect()

    assert requested == [True]
    assert [item.mount_point for item in result.items] == ["/Volumes/models"]
    assert result.items[0].filesystem_family == FilesystemFamily.NFS


def test_filters_pseudo_mounts_but_preserves_apfs_and_nfs() -> None:
    partitions = [
        SimpleNamespace(device="devfs", mountpoint="/dev", fstype="devfs", opts="rw"),
        SimpleNamespace(
            device="map auto_home",
            mountpoint="/System/Volumes/Data/home",
            fstype="autofs",
            opts="rw",
        ),
        SimpleNamespace(
            device="tmpfs", mountpoint="/dev/shm", fstype="tmpfs", opts="rw"
        ),
        SimpleNamespace(
            device="/dev/disk3s1", mountpoint="/", fstype="apfs", opts="rw"
        ),
        SimpleNamespace(
            device="server:/models",
            mountpoint="/Volumes/models",
            fstype="nfs",
            opts="rw",
        ),
    ]
    usage_calls: list[str] = []

    def usage(mountpoint: str):
        usage_calls.append(mountpoint)
        return SimpleNamespace(total=100, used=10, free=90, percent=10)

    collector = VolumeCollector(
        partitions_provider=lambda **_: partitions,
        usage_provider=usage,
        system_provider=lambda: "Linux",
        now=lambda: NOW,
    )

    result = collector.collect()

    assert usage_calls == ["/", "/Volumes/models"]
    assert [item.mount_point for item in result.items] == ["/", "/Volumes/models"]
    assert [item.filesystem_family for item in result.items] == [
        FilesystemFamily.APFS,
        FilesystemFamily.NFS,
    ]


def test_collects_capacity_and_structured_apfs_metadata() -> None:
    partition = SimpleNamespace(
        device="/dev/disk3s1",
        mountpoint="/",
        fstype="apfs",
        opts="rw,local",
    )
    diskutil_calls: list[tuple[str, float]] = []

    def diskutil(target: str, timeout: float):
        diskutil_calls.append((target, timeout))
        return {
            "VolumeName": "Macintosh HD",
            "FilesystemType": "apfs",
            "APFSContainerReference": "disk3",
            "VolumeUUID": "ABC-123",
            "APFSVolumeRole": ["System"],
            "APFSEncrypted": True,
            "SMARTStatus": "Verified",
        }

    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=1_000_000, used=250_000, free=750_000, percent=25
        ),
        system_provider=lambda: "Darwin",
        diskutil_runner=diskutil,
        now=lambda: NOW,
    )

    result = collector.collect()

    assert result.status == CapabilityStatus.AVAILABLE
    assert result.source.endswith("+diskutil_plist")
    assert diskutil_calls == [("/dev/disk3s1", 2.0)]
    volume = result.items[0]
    assert volume.name == "Macintosh HD"
    assert volume.filesystem_family == FilesystemFamily.APFS
    assert volume.total_bytes == 1_000_000
    assert volume.used_percent == 25
    assert volume.health.status == CapabilityStatus.AVAILABLE
    assert volume.health.smart_status == "Verified"
    assert volume.apfs is not None
    assert volume.apfs.container_reference == "disk3"
    assert volume.apfs.roles == ["System"]
    assert volume.apfs.encrypted is True


def test_nfs_is_remote_and_does_not_claim_server_disk_health() -> None:
    partition = SimpleNamespace(
        device="server.example:/models",
        mountpoint="/Volumes/models",
        fstype="nfs",
        opts="ro,nosuid",
    )
    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=2_000, used=500, free=1_500, percent=25
        ),
        system_provider=lambda: "Darwin",
        diskutil_runner=lambda *_: (_ for _ in ()).throw(
            AssertionError("diskutil must not inspect a remote export")
        ),
        now=lambda: NOW,
    )

    result = collector.collect()

    volume = result.items[0]
    assert volume.filesystem_family == FilesystemFamily.NFS
    assert volume.remote is True
    assert volume.read_only is True
    assert volume.health.status == CapabilityStatus.UNAVAILABLE
    assert "NFS server" in (volume.health.message or "")
    assert volume.nfs is not None
    assert volume.nfs.server == "server.example"
    assert volume.nfs.export == "/models"
    assert volume.nfs.mount_options == ["nosuid", "ro"]
    assert volume.nfs.pnfs_status == CapabilityStatus.UNAVAILABLE
    assert "does not support pNFS" in volume.nfs.pnfs_message


def test_nfsstat_current_json_enriches_only_allowlisted_metadata() -> None:
    partition = SimpleNamespace(
        device="fallback.example:/old",
        mountpoint="/Volumes/models",
        fstype="nfs",
        opts=(
            "rw,hard,principal=user@SECRET.REALM,realm=SECRET.REALM,"
            "sprincipal=nfs/server"
        ),
    )
    calls: list[tuple[str, float]] = []

    def nfsstat(target: str, timeout: float):
        calls.append((target, timeout))
        return {
            "fallback.example:/old": {
                "Mount Point": "/Volumes/models",
                "Current mount parameters": {
                    "NFS parameters": [
                        "vers=4.1",
                        "proto=tcp6",
                        "rwsize=1048576",
                        "sec=krb5p",
                        "principal=secret@example.com",
                        "fpnfs",
                    ],
                    "File system locations": [
                        {
                            "Server": "current.example",
                            "Export": "/models",
                            "Locations": ["2001:db8::1"],
                        }
                    ],
                },
                "Status flags": {
                    "Bitmask": "0xff",
                    "Flags": ["not responding", "recovery", "private flag"],
                },
                "filehandle": {"Handle": "must-not-leak"},
            }
        }

    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=2_000, used=500, free=1_500, percent=25
        ),
        system_provider=lambda: "Darwin",
        diskutil_runner=lambda *_: (_ for _ in ()).throw(
            AssertionError("diskutil must not inspect NFS")
        ),
        nfsstat_runner=nfsstat,
        now=lambda: NOW,
    )

    result = collector.collect()

    assert result.status == CapabilityStatus.AVAILABLE
    assert result.source.endswith("+nfsstat_json")
    assert calls == [("/Volumes/models", 2.0)]
    details = result.items[0].nfs
    assert details is not None
    assert details.server == "current.example"
    assert details.export == "/models"
    assert details.protocol_version == "4.1"
    assert details.mount_options == [
        "hard",
        "proto=tcp6",
        "rw",
        "rwsize=1048576",
        "sec=krb5p",
        "vers=4.1",
    ]
    assert details.status_flags == ["not responding", "recovery"]
    serialized_options = " ".join(details.mount_options).casefold()
    assert "principal" not in serialized_options
    assert "realm" not in serialized_options
    assert "secret" not in serialized_options
    assert details.pnfs_status == CapabilityStatus.UNAVAILABLE


def test_nfsstat_never_attaches_a_different_mounts_metadata() -> None:
    partition = SimpleNamespace(
        device="right.example:/right",
        mountpoint="/Volumes/right",
        fstype="nfs",
        opts="rw,vers=3",
    )
    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=100, used=10, free=90, percent=10
        ),
        system_provider=lambda: "Darwin",
        nfsstat_runner=lambda *_: {
            "wrong.example:/wrong": {
                "Mount Point": "/Volumes/wrong",
                "Current mount parameters": {
                    "NFS parameters": ["vers=4.1"],
                },
            }
        },
        now=lambda: NOW,
    )

    result = collector.collect()

    assert result.status == CapabilityStatus.PARTIAL
    details = result.items[0].nfs
    assert details is not None
    assert details.server == "right.example"
    assert details.export == "/right"
    assert details.protocol_version == "3"
    assert "nfsstat metadata unavailable" in (result.message or "")


def test_bracketed_ipv6_source_and_absent_protocol_are_conservative() -> None:
    partition = SimpleNamespace(
        device="[2001:db8::1]:/models",
        mountpoint="/Volumes/models",
        fstype="NFS",
        opts="rw",
    )
    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=100, used=10, free=90, percent=10
        ),
        system_provider=lambda: "Linux",
        now=lambda: NOW,
    )

    details = collector.collect().items[0].nfs

    assert details is not None
    assert details.server == "2001:db8::1"
    assert details.export == "/models"
    assert details.protocol_version is None


def test_raw_nfsvers_and_minorversion_are_preserved_as_explicit_version() -> None:
    partition = SimpleNamespace(
        device="server:/models",
        mountpoint="/Volumes/models",
        fstype="nfs",
        opts="rw,nfsvers=4,minorversion=1",
    )
    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=100, used=10, free=90, percent=10
        ),
        system_provider=lambda: "Linux",
        now=lambda: NOW,
    )

    details = collector.collect().items[0].nfs

    assert details is not None
    assert details.protocol_version == "4.1"
    assert details.mount_options == ["minorversion=1", "nfsvers=4", "rw"]


def test_nfsstat_metadata_is_cached_per_mount() -> None:
    partition = SimpleNamespace(
        device="server:/models",
        mountpoint="/Volumes/models",
        fstype="nfs",
        opts="rw",
    )
    calls = 0
    current_time = [10.0]

    def nfsstat(*_):
        nonlocal calls
        calls += 1
        return {
            "server:/models": {
                "Mount Point": "/Volumes/models",
                "Current mount parameters": {"NFS parameters": ["vers=3"]},
            }
        }

    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=100, used=10, free=90, percent=10
        ),
        system_provider=lambda: "Darwin",
        nfsstat_runner=nfsstat,
        monotonic=lambda: current_time[0],
        nfsstat_cache_ttl_seconds=45,
        now=lambda: NOW,
    )

    collector.collect()
    collector.collect()
    assert calls == 1
    current_time[0] = 55
    collector.collect()
    assert calls == 2


def test_capacity_failure_is_reported_not_fabricated() -> None:
    partition = SimpleNamespace(
        device="/dev/disk3s1", mountpoint="/private", fstype="apfs", opts="rw"
    )
    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: (_ for _ in ()).throw(PermissionError("denied")),
        system_provider=lambda: "Darwin",
        now=lambda: NOW,
    )

    result = collector.collect()

    assert result.status == CapabilityStatus.ERROR
    assert result.items == []
    assert "No mounted volume had readable capacity" in (result.message or "")


def test_diskutil_metadata_is_cached_while_capacity_stays_fresh() -> None:
    partition = SimpleNamespace(
        device="/dev/disk3s1", mountpoint="/", fstype="apfs", opts="rw"
    )
    calls = 0
    current_time = [10.0]

    def diskutil(*_):
        nonlocal calls
        calls += 1
        return {"FilesystemType": "apfs", "SMARTStatus": "Verified"}

    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=100, used=25, free=75, percent=25
        ),
        system_provider=lambda: "Darwin",
        diskutil_runner=diskutil,
        monotonic=lambda: current_time[0],
        diskutil_cache_ttl_seconds=45,
        now=lambda: NOW,
    )

    collector.collect()
    collector.collect()
    assert calls == 1

    current_time[0] = 55.0
    collector.collect()
    assert calls == 2


@pytest.mark.parametrize(
    ("smart_value", "expected"),
    [
        ("Verified", CapabilityStatus.AVAILABLE),
        ("Passed", CapabilityStatus.AVAILABLE),
        ("Not Supported", CapabilityStatus.UNAVAILABLE),
        ("Unknown", CapabilityStatus.UNAVAILABLE),
        ("Failing", CapabilityStatus.ERROR),
        ("Warning", CapabilityStatus.PARTIAL),
    ],
)
def test_smart_status_is_normalized_conservatively(
    smart_value: str, expected: CapabilityStatus
) -> None:
    partition = SimpleNamespace(
        device="/dev/disk3s1", mountpoint="/", fstype="apfs", opts="rw"
    )
    collector = VolumeCollector(
        partitions_provider=lambda **_: [partition],
        usage_provider=lambda _: SimpleNamespace(
            total=100, used=25, free=75, percent=25
        ),
        system_provider=lambda: "Darwin",
        diskutil_runner=lambda *_: {
            "FilesystemType": "apfs",
            "SMARTStatus": smart_value,
        },
        now=lambda: NOW,
    )

    result = collector.collect()

    assert result.items[0].health.status == expected
    assert result.items[0].health.smart_status == smart_value
