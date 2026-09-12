from __future__ import annotations

from datetime import datetime, timedelta, timezone

from localtrace_backend.capacity_alerts import CapacityAlertService
from localtrace_backend.models import (
    APFSDetails,
    CapabilityStatus,
    FilesystemFamily,
    Volume,
    VolumeHealth,
    VolumesResponse,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=1)
        return result


def volume(
    identifier: str,
    percent: float,
    *,
    container: str | None = None,
    total: int = 1_000,
    used: int | None = None,
    available: int | None = None,
    name: str | None = None,
    mount_point: str | None = None,
    roles: list[str] | None = None,
    read_only: bool = False,
) -> Volume:
    used_bytes = int(total * percent / 100) if used is None else used
    available_bytes = (
        max(0, total - used_bytes) if available is None else available
    )
    return Volume(
        id=identifier,
        name=name or f"Volume {identifier}",
        mount_point=mount_point or f"/Volumes/{identifier}",
        device=f"/dev/{identifier}",
        filesystem="apfs" if container else "other",
        filesystem_family=(
            FilesystemFamily.APFS if container else FilesystemFamily.OTHER
        ),
        remote=False,
        read_only=read_only,
        total_bytes=total,
        used_bytes=used_bytes,
        available_bytes=available_bytes,
        used_percent=percent,
        health=VolumeHealth(status=CapabilityStatus.UNAVAILABLE),
        apfs=(
            APFSDetails(container_reference=container, roles=roles or [])
            if container
            else None
        ),
    )


def snapshot(*items: Volume, status=CapabilityStatus.AVAILABLE) -> VolumesResponse:
    return VolumesResponse(
        sampled_at=datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc),
        status=status,
        source="test",
        items=list(items),
    )


def test_exact_threshold_alerts_once_and_copies_real_capacity() -> None:
    service = CapacityAlertService(threshold_percent=90, now=Clock())

    first = service.evaluate(snapshot(volume("disk1", 90)))
    duplicate = service.evaluate(snapshot(volume("disk1", 97)))

    assert len(first) == 1
    assert duplicate == []
    alert = first[0]
    assert alert.rule == "CAPACITY_PRESSURE"
    assert alert.path == "/Volumes/disk1"
    assert alert.observed_percent == 90
    assert alert.threshold_percent == 90
    assert alert.used_bytes == 900
    assert alert.available_bytes == 100
    assert alert.total_bytes == 1_000
    assert alert.related_event_ids == []
    assert len(service.snapshot()[2]) == 1


def test_rearms_only_after_real_recovery_below_hysteresis() -> None:
    service = CapacityAlertService(
        threshold_percent=90,
        rearm_percent=88,
        now=Clock(),
    )
    assert len(service.evaluate(snapshot(volume("disk1", 91)))) == 1
    assert service.evaluate(snapshot(volume("disk1", 89))) == []
    assert service.evaluate(snapshot(volume("disk1", 91))) == []
    assert service.evaluate(snapshot(volume("disk1", 88))) == []
    assert len(service.evaluate(snapshot(volume("disk1", 91)))) == 1


def test_error_or_disappearance_does_not_fabricate_recovery() -> None:
    service = CapacityAlertService(threshold_percent=90, now=Clock())
    assert len(service.evaluate(snapshot(volume("disk1", 95)))) == 1

    assert service.evaluate(snapshot(status=CapabilityStatus.ERROR)) == []
    assert service.evaluate(snapshot()) == []
    assert service.evaluate(snapshot(volume("disk1", 95))) == []
    assert service.snapshot()[0] == CapabilityStatus.AVAILABLE


def test_independent_volumes_alert_but_apfs_siblings_are_suppressed() -> None:
    service = CapacityAlertService(threshold_percent=90, now=Clock())

    emitted = service.evaluate(
        snapshot(
            volume("system", 93, container="disk3"),
            volume("data", 94, container="disk3"),
            volume("external", 96),
        )
    )

    assert len(emitted) == 2
    assert {item.volume_id for item in emitted} == {"data", "external"}


def test_apfs_pressure_uses_shared_space_and_prefers_data_volume() -> None:
    total = 245_107_195_904
    available = 15_125_999_616
    system = volume(
        "system",
        44.8,
        container="disk3",
        total=total,
        used=12_267_216_896,
        available=available,
        name="Macintosh HD",
        mount_point="/",
        read_only=True,
    )
    data = volume(
        "data",
        93.1,
        container="disk3",
        total=total,
        used=202_960_592_896,
        available=available,
        name="Data",
        mount_point="/System/Volumes/Data",
    )
    service = CapacityAlertService(threshold_percent=93.5, now=Clock())

    emitted = service.evaluate(snapshot(system, data))

    assert len(emitted) == 1
    alert = emitted[0]
    expected_used = total - available
    assert alert.volume_id == "data"
    assert alert.mount_point == "/System/Volumes/Data"
    assert alert.used_bytes == expected_used
    assert alert.available_bytes == available
    assert alert.observed_percent == round(expected_used / total * 100, 2)
    assert alert.title == "APFS container capacity pressure detected"
    assert "APFS container disk3" in alert.message
    assert "representative mounted volume" in alert.message
    # Alert derivation must not rewrite frontend-facing per-volume allocation.
    assert system.used_bytes == 12_267_216_896
    assert system.used_percent == 44.8
    assert data.used_bytes == 202_960_592_896
    assert data.used_percent == 93.1


def test_apfs_representative_is_stable_and_falls_back_to_data_role() -> None:
    system = volume(
        "z-system",
        91,
        container="disk3",
        name="System",
        mount_point="/custom/system",
        roles=["System"],
    )
    data = volume(
        "a-data",
        91,
        container="disk3",
        name="Writable",
        mount_point="/custom/writable",
        roles=["Data"],
    )

    first = CapacityAlertService(threshold_percent=90, now=Clock()).evaluate(
        snapshot(system, data)
    )
    reversed_order = CapacityAlertService(
        threshold_percent=90, now=Clock()
    ).evaluate(snapshot(data, system))

    assert first[0].volume_id == "a-data"
    assert reversed_order[0].volume_id == "a-data"


def test_zero_total_is_ignored_and_history_is_bounded() -> None:
    service = CapacityAlertService(threshold_percent=90, now=Clock(), max_alerts=2)
    assert service.evaluate(snapshot(volume("zero", 100, total=0))) == []

    for identifier in ("one", "two", "three"):
        assert len(service.evaluate(snapshot(volume(identifier, 95)))) == 1
    assert [item.volume_id for item in service.snapshot()[2]] == ["three", "two"]


def test_environment_configuration_is_validated(monkeypatch) -> None:
    monkeypatch.setenv("LOCALTRACE_CAPACITY_ALERT_PERCENT", "not-a-number")
    monkeypatch.setenv("LOCALTRACE_CAPACITY_REARM_PERCENT", "200")

    service = CapacityAlertService.from_environment()
    service.evaluate(snapshot(volume("disk1", 95)))

    assert service.threshold_percent == 90
    assert service.rearm_percent == 88
    assert service.snapshot()[0] == CapabilityStatus.PARTIAL
    assert "invalid" in (service.snapshot()[1] or "")
