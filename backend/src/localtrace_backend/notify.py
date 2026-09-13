"""Deliver raised alerts somewhere an administrator will see them.

The dashboard only helps someone who is looking at it. This service forwards
every new alert to two sinks: macOS Notification Center through ``osascript``
and an append-only JSON Lines log that another tool can tail or ship.

Delivery never runs on the thread that raised the alert. Alerts are queued and
a worker drains them, so a slow ``osascript`` call cannot delay the FSEvents
handler or a dashboard request. A sink failure is recorded as capability
state; it never causes an alert to be dropped from the in-memory store.

Safety rules:

- ``osascript`` is invoked with an argument array, a timeout, and the alert
  text passed as script *arguments* (``on run argv``), never interpolated into
  the AppleScript source. A path containing quotes cannot change the script.
- The log file is opened ``O_APPEND | O_CREAT | O_NOFOLLOW`` with mode 0600,
  so an existing symbolic link at the path is refused rather than followed.
- Each alert id is delivered at most once per process, and only ever on the
  worker thread; ``deliver`` merely enqueues.
- Per-sink outcomes are tracked separately, so a sink that is available but
  failed its last post is reported as ``error`` rather than ``on``.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from localtrace_backend.models import (
    Alert,
    AlertDeliveryStatus,
    AlertSinkStatus,
    CapabilityStatus,
)

OSASCRIPT = "/usr/bin/osascript"
NOTIFICATION_TIMEOUT_SECONDS = 5.0
MAX_MESSAGE_CHARS = 240
MAX_RECENT_DELIVERIES = 200
SOURCE = "osascript-notification+jsonl-log"

NotificationRunner = Callable[[Sequence[str], float], Any]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: object, limit: int = MAX_MESSAGE_CHARS) -> str:
    return " ".join(str(value).split())[:limit]


def default_log_path(system: str | None = None) -> str:
    """Pick a log location an administrator would expect on the platform."""

    system = system or platform.system()
    if system == "Darwin":
        return str(Path.home() / "Library" / "Logs" / "LocalTrace" / "alerts.jsonl")
    import tempfile

    return str(Path(tempfile.gettempdir()) / "localtrace" / "alerts.jsonl")


def _run_osascript(argv: Sequence[str], timeout: float) -> Any:
    return subprocess.run(  # noqa: S603 - argument array, no shell
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class NotificationCenterSink:
    """Post a macOS Notification Center banner for one alert."""

    name = "notification_center"

    def __init__(
        self,
        *,
        enabled: bool = True,
        system_provider: Callable[[], str] = platform.system,
        runner: NotificationRunner = _run_osascript,
        executable: str = OSASCRIPT,
        timeout: float = NOTIFICATION_TIMEOUT_SECONDS,
    ) -> None:
        self._enabled = enabled
        self._system = system_provider()
        self._runner = runner
        self._executable = executable
        self._timeout = timeout

    def status(self) -> AlertSinkStatus:
        if not self._enabled:
            return AlertSinkStatus(
                name=self.name,
                status=CapabilityStatus.UNAVAILABLE,
                target=None,
                message="Disabled by LOCALTRACE_NOTIFY=0.",
            )
        if self._system != "Darwin":
            return AlertSinkStatus(
                name=self.name,
                status=CapabilityStatus.UNAVAILABLE,
                target=None,
                message="Notification Center delivery is macOS-only.",
            )
        if not os.path.isfile(self._executable):
            return AlertSinkStatus(
                name=self.name,
                status=CapabilityStatus.UNAVAILABLE,
                target=None,
                message=f"{self._executable} was not found.",
            )
        return AlertSinkStatus(
            name=self.name,
            status=CapabilityStatus.AVAILABLE,
            target=self._executable,
            message="Banners post through osascript; the alert text is passed as arguments.",
        )

    def deliver(self, alert: Alert) -> None:
        current = self.status()
        if current.status != CapabilityStatus.AVAILABLE:
            return
        argv = [
            self._executable,
            "-e",
            "on run argv",
            "-e",
            "display notification (item 1 of argv) with title (item 2 of argv) "
            "subtitle (item 3 of argv)",
            "-e",
            "end run",
            "--",
            _clean(alert.message),
            "LocalTrace",
            _clean(alert.title, 80),
        ]
        result = self._runner(argv, self._timeout)
        returncode = getattr(result, "returncode", 0)
        if returncode != 0:
            stderr = _clean(getattr(result, "stderr", "") or "")
            raise RuntimeError(f"osascript exited {returncode}: {stderr or 'no diagnostic'}")


class JsonlLogSink:
    """Append one JSON object per alert to a local, user-owned log file."""

    name = "jsonl_log"

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        self._lock = threading.Lock()

    def status(self) -> AlertSinkStatus:
        return AlertSinkStatus(
            name=self.name,
            status=CapabilityStatus.AVAILABLE,
            target=self.path,
            message="Append-only JSON Lines; one object per alert.",
        )

    def deliver(self, alert: Alert) -> None:
        record = {
            "delivered_at": utc_now().isoformat(),
            "alert": alert.model_dump(mode="json"),
        }
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        with self._lock:
            parent = Path(self.path).parent
            if parent.is_symlink():
                raise OSError("refusing a symbolic-link log directory")
            parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)


class AlertDeliveryService:
    """Queue new alerts and fan them out to every configured sink."""

    def __init__(
        self,
        *,
        sinks: Sequence[Any],
        now: Callable[[], datetime] = utc_now,
        max_recent: int = MAX_RECENT_DELIVERIES,
    ) -> None:
        self._sinks = list(sinks)
        self._now = now
        self._queue: queue.Queue[Alert | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._delivered: deque[str] = deque(maxlen=max_recent)
        self._delivered_count = 0
        self._partial_count = 0
        self._failed_count = 0
        self._last_delivered_at: datetime | None = None
        self._last_error: str | None = None
        self._sink_ok: dict[str, int] = {}
        self._sink_failed: dict[str, int] = {}
        self._sink_last_error: dict[str, str | None] = {}

    @classmethod
    def from_environment(cls) -> "AlertDeliveryService":
        raw_notify = (os.getenv("LOCALTRACE_NOTIFY") or "1").strip().lower()
        notify_enabled = raw_notify not in {"0", "false", "no", "off"}
        log_path = os.getenv("LOCALTRACE_ALERT_LOG_PATH") or default_log_path()
        return cls(
            sinks=[
                NotificationCenterSink(enabled=notify_enabled),
                JsonlLogSink(log_path),
            ]
        )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._drain, name="localtrace-alert-delivery", daemon=True
            )
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return
        self._queue.put(None)
        thread.join(timeout=3)

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._deliver_now(item)
            finally:
                self._queue.task_done()

    # -- delivery ----------------------------------------------------------

    def deliver(self, alert: Alert) -> None:
        """Enqueue one alert. Safe to call from any thread and under locks.

        Delivery always happens on the worker. If ``start`` has not been
        called yet the alert waits in the queue; nothing is ever delivered on
        the thread that raised the alert.
        """

        with self._lock:
            if alert.id in self._seen:
                return
            self._seen.add(alert.id)
        self._queue.put(alert)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued deliveries finish; returns False on timeout.

        When no worker is running (tests, or a service that was never
        started) the queue is drained on the calling thread instead.
        """

        with self._lock:
            running = self._thread is not None
        if not running:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    return True
                try:
                    if item is not None:
                        self._deliver_now(item)
                finally:
                    self._queue.task_done()
        end = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def _deliver_now(self, alert: Alert) -> None:
        succeeded: list[str] = []
        errors: list[str] = []
        for sink in self._sinks:
            name = getattr(sink, "name", "sink")
            if sink.status().status != CapabilityStatus.AVAILABLE:
                continue  # unavailable sinks are neither successes nor failures
            try:
                sink.deliver(alert)
            except Exception as exc:  # one sink must not block the others
                message = _clean(exc)
                errors.append(f"{name}: {message}")
                with self._lock:
                    self._sink_failed[name] = self._sink_failed.get(name, 0) + 1
                    self._sink_last_error[name] = message
                continue
            succeeded.append(name)
            with self._lock:
                self._sink_ok[name] = self._sink_ok.get(name, 0) + 1
                self._sink_last_error[name] = None
        with self._lock:
            if succeeded and not errors:
                self._delivered_count += 1
                self._delivered.append(alert.id)
                self._last_delivered_at = self._now()
                self._last_error = None
            elif succeeded:
                self._partial_count += 1
                self._last_delivered_at = self._now()
                self._last_error = "; ".join(errors)
            else:
                self._failed_count += 1
                self._last_error = "; ".join(errors) or "No sink accepted the alert."

    # -- status ------------------------------------------------------------

    def snapshot(self) -> AlertDeliveryStatus:
        with self._lock:
            delivered = self._delivered_count
            partial = self._partial_count
            failed = self._failed_count
            last_at = self._last_delivered_at
            last_error = self._last_error
            sink_ok = dict(self._sink_ok)
            sink_failed = dict(self._sink_failed)
            sink_last_error = dict(self._sink_last_error)
            pending = self._queue.unfinished_tasks
        sinks: list[AlertSinkStatus] = []
        for sink in self._sinks:
            base = sink.status()
            name = base.name
            error = sink_last_error.get(name)
            if base.status == CapabilityStatus.AVAILABLE and error:
                status = CapabilityStatus.ERROR
                message = f"Last delivery failed: {error}"
            else:
                status = base.status
                message = base.message
            sinks.append(
                AlertSinkStatus(
                    name=name,
                    status=status,
                    target=base.target,
                    message=message,
                    delivered_count=sink_ok.get(name, 0),
                    failed_count=sink_failed.get(name, 0),
                    last_error=error,
                )
            )
        active = [sink for sink in sinks if sink.status == CapabilityStatus.AVAILABLE]
        errored = [sink for sink in sinks if sink.status == CapabilityStatus.ERROR]
        if not active and not errored:
            status = CapabilityStatus.UNAVAILABLE
            message = "No alert delivery sink is available on this host."
        elif errored:
            status = CapabilityStatus.PARTIAL if active else CapabilityStatus.ERROR
            message = "; ".join(f"{sink.name}: {sink.last_error}" for sink in errored)
        else:
            status = CapabilityStatus.AVAILABLE
            names = ", ".join(sink.name.replace("_", " ") for sink in active)
            message = f"New alerts are delivered to {names}."
        del last_error
        return AlertDeliveryStatus(
            status=status,
            source=SOURCE,
            message=message,
            delivered_count=delivered,
            partially_delivered_count=partial,
            failed_count=failed,
            pending_count=pending,
            last_delivered_at=last_at,
            sinks=sinks,
        )
