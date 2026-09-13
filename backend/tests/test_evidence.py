from datetime import datetime, timedelta, timezone
from pathlib import Path

from localtrace_backend.evidence import FileEvidenceService
from localtrace_backend.models import (
    CapabilityStatus,
    FileEventKind,
    WatchTargetRole,
    WatchTargetStatus,
)


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


class RecordingObserver:
    """A fake watchdog observer that records what was scheduled."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.scheduled: list[str] = []
        self.started = False
        self._fail_on = fail_on or set()

    def schedule(self, _handler, path: str, recursive: bool = False) -> None:
        if path in self._fail_on:
            raise OSError("cannot watch this directory")
        self.scheduled.append(path)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return self.started


def targets_by_path(service: FileEvidenceService) -> dict[str, object]:
    return {target.path: target for target in service.events_snapshot().watch_targets}


def test_existing_model_directories_are_watched_read_only(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    present = tmp_path / "ollama-models"
    present.mkdir()
    missing = tmp_path / "huggingface-hub"
    observer = RecordingObserver()
    service = FileEvidenceService(
        watched_path=str(demo),
        additional_paths=[
            (str(present), WatchTargetRole.MODEL_DIRECTORY),
            (str(missing), WatchTargetRole.MODEL_DIRECTORY),
        ],
        observer_factory=lambda: observer,
        now=Clock(),
    )

    service.start()

    assert observer.started
    assert observer.scheduled == [str(demo), str(present)]
    assert demo.is_dir(), "the demo path is created because the workload writes there"
    assert not missing.exists(), "a missing model directory must never be created"
    targets = targets_by_path(service)
    assert targets[str(present)].status == WatchTargetStatus.WATCHING
    assert targets[str(missing)].status == WatchTargetStatus.SKIPPED
    assert "does not create" in (targets[str(missing)].message or "")
    snapshot = service.events_snapshot()
    assert snapshot.status == CapabilityStatus.AVAILABLE
    assert "Watching 2 directories" in (snapshot.message or "")


def test_missing_configured_path_is_partial_but_missing_default_is_not(
    tmp_path: Path,
) -> None:
    demo = tmp_path / "demo"
    configured_missing = tmp_path / "nowhere"
    default_missing = tmp_path / "lmstudio"
    service = FileEvidenceService(
        watched_path=str(demo),
        additional_paths=[
            (str(default_missing), WatchTargetRole.MODEL_DIRECTORY),
            (str(configured_missing), WatchTargetRole.CONFIGURED),
        ],
        observer_factory=RecordingObserver,
        now=Clock(),
    )

    service.start()

    snapshot = service.events_snapshot()
    assert snapshot.status == CapabilityStatus.PARTIAL
    assert "1 configured watch path" in (snapshot.message or "")
    assert targets_by_path(service)[str(configured_missing)].status == WatchTargetStatus.SKIPPED


def test_nested_and_symlinked_additional_paths_are_skipped(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    parent = tmp_path / "cache"
    child = parent / "huggingface" / "hub"
    child.mkdir(parents=True)
    real = tmp_path / "real-models"
    real.mkdir()
    link = tmp_path / "linked-models"
    link.symlink_to(real, target_is_directory=True)
    observer = RecordingObserver()
    service = FileEvidenceService(
        watched_path=str(demo),
        additional_paths=[
            (str(parent), WatchTargetRole.CONFIGURED),
            (str(child), WatchTargetRole.MODEL_DIRECTORY),
            (str(link), WatchTargetRole.MODEL_DIRECTORY),
            (str(parent), WatchTargetRole.CONFIGURED),
        ],
        observer_factory=lambda: observer,
        now=Clock(),
    )

    service.start()

    assert sorted(observer.scheduled) == sorted([str(demo), str(parent)])
    targets = service.events_snapshot().watch_targets
    child_target = next(t for t in targets if t.path == str(child))
    assert child_target.status == WatchTargetStatus.SKIPPED
    assert str(parent) in (child_target.message or "")
    link_target = next(t for t in targets if t.path == str(link))
    assert link_target.status == WatchTargetStatus.SKIPPED
    assert "symbolic-link" in (link_target.message or "")
    duplicate = [t for t in targets if t.path == str(parent)]
    assert [t.status for t in duplicate] == [
        WatchTargetStatus.WATCHING,
        WatchTargetStatus.SKIPPED,
    ]
    assert service.events_snapshot().status == CapabilityStatus.AVAILABLE


def test_additional_schedule_failure_degrades_to_partial_not_error(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    broken = tmp_path / "broken"
    broken.mkdir()
    observer = RecordingObserver(fail_on={str(broken)})
    service = FileEvidenceService(
        watched_path=str(demo),
        additional_paths=[(str(broken), WatchTargetRole.MODEL_DIRECTORY)],
        observer_factory=lambda: observer,
        now=Clock(),
    )

    service.start()

    assert observer.started
    snapshot = service.events_snapshot()
    assert snapshot.status == CapabilityStatus.PARTIAL
    assert targets_by_path(service)[str(broken)].status == WatchTargetStatus.ERROR


def test_events_from_an_additional_path_feed_the_same_alert_rule(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    models = tmp_path / "models"
    models.mkdir()
    service = FileEvidenceService(
        watched_path=str(demo),
        additional_paths=[(str(models), WatchTargetRole.MODEL_DIRECTORY)],
        threshold_bytes=8,
        observer_factory=RecordingObserver,
        now=Clock(),
    )
    service.start()
    target = models / "blobs"
    target.mkdir()
    blob = target / "sha256-abc"
    blob.write_bytes(b"123456789")

    service.process(FileEventKind.CREATED, str(blob))

    alerts = service.alerts_snapshot()
    assert len(alerts.items) == 1
    assert alerts.items[0].path == str(blob)
    assert alerts.watch_targets == service.events_snapshot().watch_targets


def test_additional_paths_from_environment_respects_flag_and_list() -> None:
    defaults = FileEvidenceService.additional_paths_from_environment({})
    assert defaults
    assert all(role == WatchTargetRole.MODEL_DIRECTORY for _, role in defaults)
    assert ("~/.ollama/models", WatchTargetRole.MODEL_DIRECTORY) in defaults

    disabled = FileEvidenceService.additional_paths_from_environment(
        {"LOCALTRACE_WATCH_MODEL_DIRS": "0"}
    )
    assert disabled == []

    mixed = FileEvidenceService.additional_paths_from_environment(
        {
            "LOCALTRACE_WATCH_MODEL_DIRS": "false",
            "LOCALTRACE_WATCH_PATHS": "/Volumes/Models: ~/team-models :",
        }
    )
    assert mixed == [
        ("/Volumes/Models", WatchTargetRole.CONFIGURED),
        ("~/team-models", WatchTargetRole.CONFIGURED),
    ]


def test_watch_dedup_does_not_depend_on_configuration_order(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    parent = tmp_path / "cache"
    child = parent / "huggingface" / "hub"
    child.mkdir(parents=True)
    observer = RecordingObserver()
    service = FileEvidenceService(
        watched_path=str(demo),
        additional_paths=[
            (str(child), WatchTargetRole.MODEL_DIRECTORY),  # child listed first
            (str(parent), WatchTargetRole.CONFIGURED),
        ],
        observer_factory=lambda: observer,
        now=Clock(),
    )

    service.start()

    assert sorted(observer.scheduled) == sorted([str(demo), str(parent)])
    child_target = targets_by_path(service)[str(child)]
    assert child_target.status == WatchTargetStatus.SKIPPED
    assert str(parent) in (child_target.message or "")


def test_parent_of_demo_path_is_not_double_watched(tmp_path: Path) -> None:
    demo = tmp_path / "scratch" / "localtrace-demo"
    observer = RecordingObserver()
    service = FileEvidenceService(
        watched_path=str(demo),
        additional_paths=[(str(tmp_path / "scratch"), WatchTargetRole.CONFIGURED)],
        observer_factory=lambda: observer,
        now=Clock(),
    )

    service.start()

    # The configured parent is watched; the demo path is covered by it and
    # not scheduled a second time, so each demo write is reported once.
    assert observer.scheduled == [str(tmp_path / "scratch")]
    parent = targets_by_path(service)[str(tmp_path / "scratch")]
    assert parent.status == WatchTargetStatus.WATCHING
    demo_target = targets_by_path(service)[str(demo)]
    assert demo_target.status == WatchTargetStatus.SKIPPED
    assert str(tmp_path / "scratch") in (demo_target.message or "")
    assert demo.is_dir()
    assert service.events_snapshot().watch_targets[0].role == WatchTargetRole.DEMO
    assert service.events_snapshot().status == CapabilityStatus.AVAILABLE
    assert service.watched_directories() == [str(tmp_path / "scratch")]
