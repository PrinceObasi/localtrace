"""Bounded file-change evidence and deterministic rapid-growth alerts."""

from __future__ import annotations

import os
import platform
import tempfile
import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from localtrace_backend.models import (
    Alert,
    AlertsResponse,
    CapabilityStatus,
    EventsResponse,
    FileEvent,
    FileEventKind,
    RapidFileGrowthAlert,
)

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
    from watchdog.observers import Observer as WatchdogObserver
except ImportError:  # pragma: no cover - exercised through dependency injection
    FileSystemEvent = Any  # type: ignore[assignment,misc]
    FileSystemMovedEvent = Any  # type: ignore[assignment,misc]

    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

    WatchdogObserver = None  # type: ignore[assignment,misc]


DEFAULT_WATCH_PATH = str(Path(tempfile.gettempdir()) / "localtrace-demo")
DEFAULT_GROWTH_THRESHOLD_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 500
MAX_ALERTS = 100
MAX_TRACKED_PATHS = 2_048
MAX_RELATED_EVENTS = 32
_DEFAULT_OBSERVER = object()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_message(value: object, limit: int = 240) -> str:
    return " ".join(str(value).split())[:limit]


def _owner_name(uid: int) -> str | None:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, OSError):
        return None


@dataclass(frozen=True)
class _Metadata:
    size: int
    owner_uid: int
    owner_name: str | None


def _metadata(path: str) -> _Metadata | None:
    """Read evidence without following a possibly replaced symbolic link."""

    try:
        result = os.lstat(path)
    except OSError:
        return None
    return _Metadata(
        size=max(0, int(result.st_size)),
        owner_uid=int(result.st_uid),
        owner_name=_owner_name(int(result.st_uid)),
    )


@dataclass
class _PathState:
    size: int | None
    baseline_size: int | None
    owner_uid: int | None
    owner_name: str | None
    alerted: bool = False
    event_ids: deque[str] = field(
        default_factory=lambda: deque(maxlen=MAX_RELATED_EVENTS)
    )


class _WatchHandler(FileSystemEventHandler):
    def __init__(self, service: "FileEvidenceService") -> None:
        super().__init__()
        self._service = service

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._service.process(FileEventKind.CREATED, event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._service.process(FileEventKind.MODIFIED, event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._service.process(FileEventKind.DELETED, event.src_path)

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        if not event.is_directory:
            self._service.process_moved(event.src_path, event.dest_path)


class FileEvidenceService:
    """Own the observer lifecycle plus bounded event and alert state.

    Owner fields describe the file's ownership at observation time. They are
    evidence for investigation, not proof of which process or person wrote it.
    """

    def __init__(
        self,
        *,
        watched_path: str = DEFAULT_WATCH_PATH,
        threshold_bytes: int = DEFAULT_GROWTH_THRESHOLD_BYTES,
        system_provider: Callable[[], str] = platform.system,
        observer_factory: Any = _DEFAULT_OBSERVER,
        now: Callable[[], datetime] = utc_now,
        max_events: int = MAX_EVENTS,
        max_alerts: int = MAX_ALERTS,
        max_tracked_paths: int = MAX_TRACKED_PATHS,
        configuration_message: str | None = None,
    ) -> None:
        if threshold_bytes <= 0:
            raise ValueError("threshold_bytes must be positive")
        if min(max_events, max_alerts, max_tracked_paths) <= 0:
            raise ValueError("store limits must be positive")

        self.watched_path = os.path.abspath(os.path.expanduser(watched_path))
        self.threshold_bytes = threshold_bytes
        self.source = (
            "watchdog-fsevents" if system_provider() == "Darwin" else "watchdog"
        )
        self._observer_factory = (
            WatchdogObserver if observer_factory is _DEFAULT_OBSERVER else observer_factory
        )
        self._now = now
        self._events: deque[FileEvent] = deque(maxlen=max_events)
        self._alerts: deque[Alert] = deque(maxlen=max_alerts)
        self._paths: OrderedDict[str, _PathState] = OrderedDict()
        self._max_tracked_paths = max_tracked_paths
        self._status = CapabilityStatus.WARMING_UP
        self._message = configuration_message or "Watcher has not started."
        self._configuration_message = configuration_message
        self._observer: Any = None
        self._lock = threading.RLock()

    @classmethod
    def from_environment(cls) -> "FileEvidenceService":
        watched_path = os.getenv("LOCALTRACE_WATCH_PATH") or DEFAULT_WATCH_PATH
        raw_threshold = os.getenv("LOCALTRACE_FILE_GROWTH_ALERT_BYTES")
        threshold = DEFAULT_GROWTH_THRESHOLD_BYTES
        configuration_message: str | None = None
        if raw_threshold:
            try:
                parsed = int(raw_threshold)
                if parsed <= 0:
                    raise ValueError
                threshold = parsed
            except ValueError:
                configuration_message = (
                    "LOCALTRACE_FILE_GROWTH_ALERT_BYTES was invalid; using the "
                    f"default of {DEFAULT_GROWTH_THRESHOLD_BYTES} bytes."
                )
        return cls(
            watched_path=watched_path,
            threshold_bytes=threshold,
            configuration_message=configuration_message,
        )

    def start(self) -> None:
        """Start once; convert every startup failure into capability state."""

        with self._lock:
            if self._observer is not None:
                return
            if self._observer_factory is None:
                self._status = CapabilityStatus.UNAVAILABLE
                self._message = "The watchdog observer backend is unavailable."
                return

        try:
            path = Path(self.watched_path)
            if path.is_symlink():
                raise OSError("refusing to watch a symbolic-link directory")
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise OSError("refusing to watch a symbolic-link directory")
            if not path.is_dir():
                raise NotADirectoryError(f"{self.watched_path} is not a directory")
            observer = self._observer_factory()
            observer.schedule(_WatchHandler(self), self.watched_path, recursive=True)
            observer.start()
        except Exception as exc:  # observer implementations expose platform-specific errors
            with self._lock:
                self._status = CapabilityStatus.ERROR
                self._message = f"File watcher could not start: {_clean_message(exc)}"
            return

        with self._lock:
            self._observer = observer
            if self._configuration_message:
                self._status = CapabilityStatus.PARTIAL
                self._message = self._configuration_message
            else:
                self._status = CapabilityStatus.AVAILABLE
                self._message = (
                    "Owner fields are file-ownership evidence, not writer identity."
                )

    def stop(self) -> None:
        with self._lock:
            observer = self._observer
            self._observer = None
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=3)
        except Exception:
            # Shutdown must not make the application hang or mask another error.
            return

    def _touch_path(self, path: str, state: _PathState) -> None:
        self._paths[path] = state
        self._paths.move_to_end(path)
        while len(self._paths) > self._max_tracked_paths:
            self._paths.popitem(last=False)

    def _new_event(
        self,
        *,
        kind: FileEventKind,
        path: str,
        destination_path: str | None,
        before: int | None,
        after: int | None,
        owner_uid: int | None,
        owner_name: str | None,
        observed_at: datetime,
    ) -> FileEvent:
        delta = after - before if before is not None and after is not None else None
        if kind == FileEventKind.CREATED and after is not None:
            before = 0
            delta = after
        elif kind == FileEventKind.DELETED and before is not None:
            after = None
            delta = -before
        return FileEvent(
            id=uuid.uuid4().hex,
            observed_at=observed_at,
            path=path,
            destination_path=destination_path,
            kind=kind,
            owner_uid=owner_uid,
            owner_name=owner_name,
            size_before=before,
            size_after=after,
            delta_bytes=delta,
            source=self.source,
        )

    def _maybe_alert(
        self, path: str, state: _PathState, event: FileEvent
    ) -> Alert | None:
        if (
            state.alerted
            or state.size is None
            or state.baseline_size is None
        ):
            return None
        growth = max(0, state.size - state.baseline_size)
        if growth < self.threshold_bytes:
            return None

        state.alerted = True
        alert = RapidFileGrowthAlert(
            id=uuid.uuid4().hex,
            title="Rapid file growth observed",
            message=(
                f"{path} grew by {growth} bytes after it entered the watch window. "
                "File ownership is evidence and does not identify the writer."
            ),
            occurred_at=event.observed_at,
            path=path,
            threshold_bytes=self.threshold_bytes,
            observed_growth_bytes=growth,
            related_event_ids=list(state.event_ids),
        )
        self._alerts.append(alert)
        return alert

    def process(self, kind: FileEventKind, path: str) -> FileEvent:
        """Normalize a watcher callback and atomically update evidence state."""

        normalized = os.path.abspath(path)
        observed_at = self._now()
        metadata = None if kind == FileEventKind.DELETED else _metadata(normalized)

        with self._lock:
            prior = self._paths.get(normalized)
            before = prior.size if prior else None
            after = metadata.size if metadata else None
            owner_uid = metadata.owner_uid if metadata else (prior.owner_uid if prior else None)
            owner_name = (
                metadata.owner_name if metadata else (prior.owner_name if prior else None)
            )
            event = self._new_event(
                kind=kind,
                path=normalized,
                destination_path=None,
                before=before,
                after=after,
                owner_uid=owner_uid,
                owner_name=owner_name,
                observed_at=observed_at,
            )
            self._events.append(event)

            if kind == FileEventKind.DELETED:
                self._paths.pop(normalized, None)
                return event

            if prior is None:
                # Creation proves the path did not previously exist; an unknown
                # modified path does not give us an honest zero-byte baseline.
                baseline = 0 if kind == FileEventKind.CREATED else after
                state = _PathState(after, baseline, owner_uid, owner_name)
            else:
                state = prior
                state.size = after
                state.owner_uid = owner_uid
                state.owner_name = owner_name
            state.event_ids.append(event.id)
            self._touch_path(normalized, state)
            self._maybe_alert(normalized, state, event)
            return event

    def process_moved(self, source_path: str, destination_path: str) -> FileEvent:
        source = os.path.abspath(source_path)
        destination = os.path.abspath(destination_path)
        observed_at = self._now()
        metadata = _metadata(destination)

        with self._lock:
            state = self._paths.pop(source, None)
            before = state.size if state else None
            after = metadata.size if metadata else None
            owner_uid = metadata.owner_uid if metadata else (state.owner_uid if state else None)
            owner_name = (
                metadata.owner_name if metadata else (state.owner_name if state else None)
            )
            event = self._new_event(
                kind=FileEventKind.MOVED,
                path=source,
                destination_path=destination,
                before=before,
                after=after,
                owner_uid=owner_uid,
                owner_name=owner_name,
                observed_at=observed_at,
            )
            self._events.append(event)
            if state is None:
                state = _PathState(after, after, owner_uid, owner_name)
            else:
                state.size = after
                state.owner_uid = owner_uid
                state.owner_name = owner_name
            state.event_ids.append(event.id)
            self._touch_path(destination, state)
            self._maybe_alert(destination, state, event)
            return event

    def _refresh_observer_status(self) -> None:
        with self._lock:
            observer = self._observer
            status = self._status
        if observer is None or status not in {
            CapabilityStatus.AVAILABLE,
            CapabilityStatus.PARTIAL,
        }:
            return
        try:
            alive = observer.is_alive()
        except Exception:
            return
        if not alive:
            with self._lock:
                self._status = CapabilityStatus.ERROR
                self._message = "The file watcher stopped unexpectedly."

    def events_snapshot(self, limit: int = MAX_EVENTS) -> EventsResponse:
        self._refresh_observer_status()
        with self._lock:
            items = list(reversed(self._events))[: max(0, limit)]
            return EventsResponse(
                sampled_at=self._now(),
                status=self._status,
                source=self.source,
                message=self._message,
                watched_path=self.watched_path,
                items=items,
            )

    def alerts_snapshot(self, limit: int = MAX_ALERTS) -> AlertsResponse:
        self._refresh_observer_status()
        with self._lock:
            items = list(reversed(self._alerts))[: max(0, limit)]
            return AlertsResponse(
                sampled_at=self._now(),
                status=self._status,
                source=self.source,
                message=self._message,
                watched_path=self.watched_path,
                threshold_bytes=self.threshold_bytes,
                items=items,
            )

    def get_alert(self, alert_id: str) -> Alert | None:
        with self._lock:
            return next((alert for alert in self._alerts if alert.id == alert_id), None)
