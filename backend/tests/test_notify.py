import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from localtrace_backend.capacity_alerts import CapacityAlertService
from localtrace_backend.evidence import FileEvidenceService
from localtrace_backend.models import (
    CapabilityStatus,
    FileEventKind,
    RapidFileGrowthAlert,
    VolumesResponse,
)
from test_capacity_alerts import volume
from localtrace_backend.notify import (
    AlertDeliveryService,
    JsonlLogSink,
    NotificationCenterSink,
    default_log_path,
)

NOW = datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc)


def make_alert(alert_id: str = "a1", message: str = "grew fast") -> RapidFileGrowthAlert:
    return RapidFileGrowthAlert(
        id=alert_id,
        title="Rapid file growth observed",
        message=message,
        occurred_at=NOW,
        path="/tmp/model.gguf",
        threshold_bytes=64,
        observed_growth_bytes=128,
        related_event_ids=[],
    )


class FakeRun:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        return self


class RecordingSink:
    name = "recording"

    def __init__(self, fail: bool = False) -> None:
        self.delivered: list[str] = []
        self.fail = fail

    def status(self):
        from localtrace_backend.models import AlertSinkStatus

        return AlertSinkStatus(name=self.name, status=CapabilityStatus.AVAILABLE)

    def deliver(self, alert) -> None:
        if self.fail:
            raise RuntimeError("sink exploded")
        self.delivered.append(alert.id)


# -- Notification Center ----------------------------------------------------


def test_notification_passes_alert_text_as_arguments_not_script(tmp_path: Path) -> None:
    runner = FakeRun()
    fake_osascript = tmp_path / "osascript"
    fake_osascript.write_text("")
    sink = NotificationCenterSink(
        system_provider=lambda: "Darwin", runner=runner, executable=str(fake_osascript)
    )
    hostile = 'path "with" quotes\\ and\nnewlines'

    sink.deliver(make_alert(message=hostile))

    assert len(runner.calls) == 1
    argv, timeout = runner.calls[0]
    assert argv[0] == str(fake_osascript)
    assert timeout > 0
    script_parts = [argv[i + 1] for i, item in enumerate(argv) if item == "-e"]
    assert script_parts == [
        "on run argv",
        "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
        "end run",
    ]
    assert "--" in argv
    tail = argv[argv.index("--") + 1 :]
    assert tail[0] == 'path "with" quotes\\ and newlines'
    assert tail[1] == "LocalTrace"
    assert not any(hostile in part for part in script_parts)


def test_notification_is_unavailable_off_macos_and_when_disabled() -> None:
    linux = NotificationCenterSink(system_provider=lambda: "Linux", runner=FakeRun())
    assert linux.status().status == CapabilityStatus.UNAVAILABLE
    runner = FakeRun()
    linux.deliver(make_alert())
    assert runner.calls == []

    disabled = NotificationCenterSink(
        enabled=False, system_provider=lambda: "Darwin", runner=FakeRun()
    )
    assert disabled.status().status == CapabilityStatus.UNAVAILABLE
    assert "LOCALTRACE_NOTIFY=0" in (disabled.status().message or "")


def test_notification_nonzero_exit_raises(tmp_path: Path) -> None:
    fake = tmp_path / "osascript"
    fake.write_text("")
    sink = NotificationCenterSink(
        system_provider=lambda: "Darwin",
        runner=FakeRun(returncode=1, stderr="execution error"),
        executable=str(fake),
    )
    try:
        sink.deliver(make_alert())
    except RuntimeError as exc:
        assert "execution error" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


# -- JSONL log --------------------------------------------------------------


def test_jsonl_log_appends_one_object_per_alert_with_private_mode(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "alerts.jsonl"
    sink = JsonlLogSink(str(log))

    sink.deliver(make_alert("a1"))
    sink.deliver(make_alert("a2"))

    lines = log.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["alert"]["id"] == "a1"
    assert first["alert"]["rule"] == "RAPID_FILE_GROWTH"
    assert "delivered_at" in first
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert sink.status().status == CapabilityStatus.AVAILABLE


def test_jsonl_log_refuses_symlink_at_log_path(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        return
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    link = tmp_path / "alerts.jsonl"
    link.symlink_to(victim)
    sink = JsonlLogSink(str(link))

    try:
        sink.deliver(make_alert())
    except OSError:
        pass
    else:
        raise AssertionError("expected the symlink to be refused")

    assert victim.read_text() == "keep me"
    service = AlertDeliveryService(sinks=[sink])
    service.deliver(make_alert())
    service.flush()
    snapshot = service.snapshot()
    assert snapshot.sinks[0].status == CapabilityStatus.ERROR
    assert snapshot.sinks[0].failed_count == 1
    assert snapshot.failed_count == 1


def test_default_log_path_is_platform_appropriate() -> None:
    darwin = default_log_path("Darwin")
    assert darwin.endswith("/Library/Logs/LocalTrace/alerts.jsonl")
    other = default_log_path("Linux")
    assert other.endswith("localtrace/alerts.jsonl")


# -- Delivery service -------------------------------------------------------


def test_service_delivers_each_alert_once_and_reports_counts() -> None:
    sink = RecordingSink()
    service = AlertDeliveryService(sinks=[sink], now=lambda: NOW)

    service.deliver(make_alert("a1"))
    service.deliver(make_alert("a1"))
    service.deliver(make_alert("a2"))
    assert sink.delivered == [], "nothing is delivered on the raising thread"
    assert service.snapshot().pending_count == 2
    service.flush()

    assert sink.delivered == ["a1", "a2"]
    snapshot = service.snapshot()
    assert snapshot.status == CapabilityStatus.AVAILABLE
    assert snapshot.delivered_count == 2
    assert snapshot.failed_count == 0
    assert snapshot.pending_count == 0
    assert snapshot.last_delivered_at == NOW
    assert snapshot.sinks[0].delivered_count == 2
    assert "recording" in (snapshot.message or "")


def test_service_records_sink_failure_as_partial_without_losing_other_sinks() -> None:
    good = RecordingSink()
    bad = RecordingSink(fail=True)
    bad.name = "notification_center"
    service = AlertDeliveryService(sinks=[bad, good], now=lambda: NOW)

    service.deliver(make_alert("a1"))
    service.flush()

    assert good.delivered == ["a1"]
    snapshot = service.snapshot()
    assert snapshot.status == CapabilityStatus.PARTIAL
    assert snapshot.delivered_count == 0
    assert snapshot.partially_delivered_count == 1
    assert snapshot.failed_count == 0
    assert "sink exploded" in (snapshot.message or "")
    by_name = {sink.name: sink for sink in snapshot.sinks}
    assert by_name["notification_center"].status == CapabilityStatus.ERROR
    assert by_name["notification_center"].failed_count == 1
    assert by_name["notification_center"].last_error == "sink exploded"
    assert by_name["recording"].status == CapabilityStatus.AVAILABLE
    assert by_name["recording"].delivered_count == 1


def test_service_with_no_available_sink_is_unavailable() -> None:
    sink = NotificationCenterSink(system_provider=lambda: "Linux", runner=FakeRun())
    service = AlertDeliveryService(sinks=[sink])

    assert service.snapshot().status == CapabilityStatus.UNAVAILABLE


def test_started_service_delivers_on_worker_thread() -> None:
    sink = RecordingSink()
    service = AlertDeliveryService(sinks=[sink], now=lambda: NOW)
    service.start()
    try:
        service.deliver(make_alert("a1"))
        assert service.flush(timeout=2)
    finally:
        service.stop()

    assert sink.delivered == ["a1"]


# -- Hooks from the alert rules --------------------------------------------


def test_evidence_alert_reaches_delivery(tmp_path: Path) -> None:
    sink = RecordingSink()
    delivery = AlertDeliveryService(sinks=[sink], now=lambda: NOW)
    evidence = FileEvidenceService(
        watched_path=str(tmp_path), threshold_bytes=4, observer_factory=None
    )
    evidence.set_alert_listener(delivery.deliver)
    target = tmp_path / "model.gguf"
    target.write_bytes(b"12345")

    evidence.process(FileEventKind.CREATED, str(target))
    delivery.flush()

    alert = evidence.alerts_snapshot().items[0]
    assert sink.delivered == [alert.id]


def test_capacity_alert_reaches_delivery_and_listener_errors_are_contained() -> None:
    sink = RecordingSink()
    delivery = AlertDeliveryService(sinks=[sink], now=lambda: NOW)
    calls: list[str] = []

    def exploding(alert):
        calls.append(alert.id)
        raise RuntimeError("listener bug")

    service = CapacityAlertService(threshold_percent=90, now=lambda: NOW)
    sample = VolumesResponse(
        sampled_at=NOW,
        status=CapabilityStatus.AVAILABLE,
        source="t",
        items=[volume("disk1s1", 95.0)],
    )

    service.set_alert_listener(exploding)
    emitted = service.evaluate(sample)
    assert len(emitted) == 1
    assert calls == [emitted[0].id]
    assert service.snapshot()[2][0].id == emitted[0].id

    service.set_alert_listener(delivery.deliver)
    service.evaluate(sample)  # still above threshold: no duplicate
    delivery.flush()
    assert sink.delivered == []


def test_alerts_queued_before_start_are_delivered_by_the_worker() -> None:
    sink = RecordingSink()
    service = AlertDeliveryService(sinks=[sink], now=lambda: NOW)
    service.deliver(make_alert("early"))
    assert sink.delivered == []

    service.start()
    try:
        assert service.flush(timeout=2)
    finally:
        service.stop()

    assert sink.delivered == ["early"]
