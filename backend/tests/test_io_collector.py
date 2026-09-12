from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from localtrace_backend.collectors.io import DiskIOSampler, select_whole_disks
from localtrace_backend.models import CapabilityStatus


NOW = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)


def counter(read: int, write: int) -> SimpleNamespace:
    return SimpleNamespace(read_bytes=read, write_bytes=write)


def test_sampler_uses_two_real_counter_snapshots_for_rates() -> None:
    snapshots = iter(
        [
            {"disk0": counter(1_000, 2_000), "disk0s1": counter(900, 1_800)},
            {"disk0": counter(3_000, 8_000), "disk0s1": counter(2_800, 7_000)},
        ]
    )
    times = iter([10.0, 12.0])
    sampler = DiskIOSampler(
        counters_provider=lambda **_: next(snapshots),
        system_provider=lambda: "Darwin",
        monotonic=lambda: next(times),
        now=lambda: NOW,
        min_interval_seconds=0,
    )

    first = sampler.sample()
    second = sampler.sample()

    assert first.status == CapabilityStatus.WARMING_UP
    assert first.aggregate.read_bytes_per_second == 0
    assert second.status == CapabilityStatus.AVAILABLE
    assert second.interval_seconds == 2
    assert [device.name for device in second.devices] == ["disk0"]
    assert second.aggregate.read_bytes_per_second == 1_000
    assert second.aggregate.write_bytes_per_second == 3_000


def test_sampler_reports_unavailable_instead_of_inventing_zero_activity() -> None:
    sampler = DiskIOSampler(
        counters_provider=lambda **_: None,
        now=lambda: NOW,
    )

    result = sampler.sample()

    assert result.status == CapabilityStatus.UNAVAILABLE
    assert "no disk I/O counters" in (result.message or "")
    assert result.devices == []


def test_counter_reset_is_partial_and_never_produces_negative_rate() -> None:
    snapshots = iter(
        [
            {"sda": counter(10_000, 8_000)},
            {"sda": counter(100, 10_000)},
        ]
    )
    times = iter([1.0, 2.0])
    sampler = DiskIOSampler(
        counters_provider=lambda **_: next(snapshots),
        system_provider=lambda: "Linux",
        monotonic=lambda: next(times),
        now=lambda: NOW,
        min_interval_seconds=0,
    )
    sampler.sample()

    result = sampler.sample()

    assert result.status == CapabilityStatus.PARTIAL
    assert result.aggregate.read_bytes_per_second == 0
    assert result.aggregate.write_bytes_per_second == 2_000
    assert "counter reset" in (result.message or "").lower()


@pytest.mark.parametrize(
    ("system", "names", "expected"),
    [
        ("Darwin", ["disk0", "disk0s1"], {"disk0"}),
        ("Linux", ["nvme0n1", "nvme0n1p1", "loop0"], {"nvme0n1"}),
        ("Windows", ["PhysicalDrive0"], {"PhysicalDrive0"}),
    ],
)
def test_whole_disk_selection_avoids_partition_double_counting(
    system: str, names: list[str], expected: set[str]
) -> None:
    selected, recognized = select_whole_disks(
        {name: counter(1, 1) for name in names}, system
    )

    assert recognized is True
    assert set(selected) == expected
