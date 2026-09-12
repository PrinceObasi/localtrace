from datetime import datetime, timedelta, timezone
from pathlib import Path

from localtrace_backend.evidence import FileEvidenceService
from localtrace_backend.models import CapabilityStatus, FileEventKind


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=1)
        return current


def make_service(tmp_path: Path, *, threshold: int = 8, max_events: int = 500):
    return FileEvidenceService(
        watched_path=str(tmp_path),
        threshold_bytes=threshold,
        system_provider=lambda: "Darwin",
        observer_factory=None,
        now=Clock(),
        max_events=max_events,
    )


def test_threshold_crossing_creates_one_deduplicated_alert(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    target = tmp_path / "model.gguf"
    target.write_bytes(b"1234")
    created = service.process(FileEventKind.CREATED, str(target))

    with target.open("ab") as file:
        file.write(b"56789")
    crossed = service.process(FileEventKind.MODIFIED, str(target))

    with target.open("ab") as file:
        file.write(b"abc")
    service.process(FileEventKind.MODIFIED, str(target))

    alerts = service.alerts_snapshot()
    assert len(alerts.items) == 1
    alert = alerts.items[0]
    assert alert.rule == "RAPID_FILE_GROWTH"
    assert alert.observed_growth_bytes == 9
    assert alert.related_event_ids == [created.id, crossed.id]
    assert "does not identify the writer" in alert.message


def test_delete_preserves_prior_evidence_and_resets_deduplication(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    target = tmp_path / "model.gguf"
    target.write_bytes(b"123456789")
    service.process(FileEventKind.CREATED, str(target))
    assert len(service.alerts_snapshot().items) == 1

    target.unlink()
    deleted = service.process(FileEventKind.DELETED, str(target))
    assert deleted.size_before == 9
    assert deleted.size_after is None
    assert deleted.delta_bytes == -9

    target.write_bytes(b"1234567890")
    service.process(FileEventKind.CREATED, str(target))
    assert len(service.alerts_snapshot().items) == 2


def test_event_store_is_bounded_and_newest_first(tmp_path: Path) -> None:
    service = make_service(tmp_path, threshold=1_000, max_events=2)
    target = tmp_path / "file.bin"
    target.write_bytes(b"1")
    first = service.process(FileEventKind.CREATED, str(target))
    target.write_bytes(b"12")
    second = service.process(FileEventKind.MODIFIED, str(target))
    target.write_bytes(b"123")
    third = service.process(FileEventKind.MODIFIED, str(target))

    events = service.events_snapshot()
    assert [item.id for item in events.items] == [third.id, second.id]
    assert first.id not in {item.id for item in events.items}


def test_missing_observer_is_unavailable_without_startup_exception(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    service.start()

    events = service.events_snapshot()
    alerts = service.alerts_snapshot()
    assert events.status == CapabilityStatus.UNAVAILABLE
    assert alerts.status == CapabilityStatus.UNAVAILABLE
    assert events.items == []
    assert "unavailable" in (events.message or "").lower()


def test_observer_start_failure_degrades_only_watcher_capability(tmp_path: Path) -> None:
    def broken_observer():
        raise OSError("FSEvents permission denied")

    service = FileEvidenceService(
        watched_path=str(tmp_path),
        observer_factory=broken_observer,
        now=Clock(),
    )

    service.start()

    assert service.events_snapshot().status == CapabilityStatus.ERROR
    assert "permission denied" in (service.events_snapshot().message or "")


def test_watcher_refuses_a_symbolic_link_directory(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    watched_link = tmp_path / "watched-link"
    watched_link.symlink_to(destination, target_is_directory=True)
    service = FileEvidenceService(
        watched_path=str(watched_link),
        observer_factory=lambda: (_ for _ in ()).throw(
            AssertionError("observer must not start for a symlink")
        ),
        now=Clock(),
    )

    service.start()

    assert service.events_snapshot().status == CapabilityStatus.ERROR
    assert "symbolic-link" in (service.events_snapshot().message or "")
