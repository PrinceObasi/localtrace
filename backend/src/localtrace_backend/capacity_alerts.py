"""Deterministic capacity-pressure alert evaluation from volume snapshots."""

from __future__ import annotations

import os
import threading
import uuid
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone

from localtrace_backend.models import (
    Alert,
    CapacityPressureAlert,
    CapabilityStatus,
    Volume,
    VolumesResponse,
)


DEFAULT_CAPACITY_THRESHOLD_PERCENT = 90.0
DEFAULT_CAPACITY_HYSTERESIS_PERCENT = 2.0
MAX_CAPACITY_ALERTS = 100
SOURCE = "volume-capacity+apfs-shared-capacity"
APFS_DATA_MOUNT_POINT = "/System/Volumes/Data"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _apfs_representative_key(
    volume: Volume,
) -> tuple[bool, bool, bool, bool, str, str, str]:
    """Choose one stable, administrator-meaningful row for an APFS container."""

    mount_point = os.path.normpath(volume.mount_point)
    roles = {
        role.strip().casefold()
        for role in (volume.apfs.roles if volume.apfs else [])
        if role.strip()
    }
    name = volume.name.strip().casefold()
    data_name = name == "data" or name.endswith(" - data")
    return (
        mount_point != APFS_DATA_MOUNT_POINT,
        "data" not in roles,
        not data_name,
        volume.read_only,
        mount_point.casefold(),
        name,
        volume.id,
    )


def _apfs_container_observation(members: list[Volume]) -> Volume:
    """Derive shared-container pressure without changing raw volume metrics.

    macOS APFS reports per-volume allocation in ``used_bytes`` while
    ``total_bytes`` and ``available_bytes`` describe the shared container.
    Container pressure is therefore total minus available, not the largest
    member's allocation percentage.
    """

    representative = min(members, key=_apfs_representative_key)
    used_bytes = max(0, representative.total_bytes - representative.available_bytes)
    used_percent = round(
        used_bytes / representative.total_bytes * 100,
        2,
    )
    return representative.model_copy(
        update={"used_bytes": used_bytes, "used_percent": used_percent}
    )


class CapacityAlertService:
    """Raise once per volume/APFS container crossing, then re-arm below it."""

    def __init__(
        self,
        *,
        threshold_percent: float = DEFAULT_CAPACITY_THRESHOLD_PERCENT,
        rearm_percent: float | None = None,
        now: Callable[[], datetime] = utc_now,
        max_alerts: int = MAX_CAPACITY_ALERTS,
        configuration_message: str | None = None,
        on_alert: Callable[[Alert], None] | None = None,
    ) -> None:
        if not 0 < threshold_percent <= 100:
            raise ValueError("threshold_percent must be greater than 0 and at most 100")
        if max_alerts <= 0:
            raise ValueError("max_alerts must be positive")
        self.threshold_percent = float(threshold_percent)
        default_rearm = max(
            0.0, self.threshold_percent - DEFAULT_CAPACITY_HYSTERESIS_PERCENT
        )
        self.rearm_percent = default_rearm if rearm_percent is None else float(rearm_percent)
        if not 0 <= self.rearm_percent < self.threshold_percent:
            raise ValueError("rearm_percent must be non-negative and below threshold_percent")
        self.source = SOURCE
        self._now = now
        self._alerts: deque[CapacityPressureAlert] = deque(maxlen=max_alerts)
        self._above_threshold: set[str] = set()
        self._status = CapabilityStatus.WARMING_UP
        self._message = "No volume sample has been evaluated yet."
        self._configuration_message = configuration_message
        self._lock = threading.RLock()
        self._on_alert = on_alert

    def set_alert_listener(self, listener: Callable[[Alert], None] | None) -> None:
        """Receive each newly raised alert; the listener must be quick."""

        with self._lock:
            self._on_alert = listener

    def _notify(self, alert: Alert) -> None:
        listener = self._on_alert
        if listener is None:
            return
        try:
            listener(alert)
        except Exception:
            return

    @classmethod
    def from_environment(cls) -> "CapacityAlertService":
        raw_threshold = os.getenv("LOCALTRACE_CAPACITY_ALERT_PERCENT")
        raw_rearm = os.getenv("LOCALTRACE_CAPACITY_REARM_PERCENT")
        threshold = DEFAULT_CAPACITY_THRESHOLD_PERCENT
        message: str | None = None
        if raw_threshold:
            try:
                parsed = float(raw_threshold)
                if not 0 < parsed <= 100:
                    raise ValueError
                threshold = parsed
            except ValueError:
                message = (
                    "LOCALTRACE_CAPACITY_ALERT_PERCENT was invalid; using the "
                    f"default of {DEFAULT_CAPACITY_THRESHOLD_PERCENT:g} percent."
                )
        rearm = max(0.0, threshold - DEFAULT_CAPACITY_HYSTERESIS_PERCENT)
        if raw_rearm:
            try:
                parsed_rearm = float(raw_rearm)
                if not 0 <= parsed_rearm < threshold:
                    raise ValueError
                rearm = parsed_rearm
            except ValueError:
                rearm = max(0.0, threshold - DEFAULT_CAPACITY_HYSTERESIS_PERCENT)
                suffix = (
                    "LOCALTRACE_CAPACITY_REARM_PERCENT was invalid; using "
                    f"{rearm:g} percent."
                )
                message = f"{message} {suffix}" if message else suffix
        return cls(
            threshold_percent=threshold,
            rearm_percent=rearm,
            configuration_message=message,
        )

    def _new_alert(self, volume: Volume) -> CapacityPressureAlert:
        container_reference = (
            volume.apfs.container_reference if volume.apfs else None
        )
        if container_reference:
            title = "APFS container capacity pressure detected"
            message = (
                f"APFS container {container_reference} is "
                f"{volume.used_percent:g}% allocated, meeting the configured "
                f"{self.threshold_percent:g}% warning threshold. "
                f"{volume.name} is the representative mounted volume."
            )
        else:
            title = "Volume capacity pressure detected"
            message = (
                f"{volume.name} is {volume.used_percent:g}% full, meeting the "
                f"configured {self.threshold_percent:g}% warning threshold."
            )
        return CapacityPressureAlert(
            id=uuid.uuid4().hex,
            title=title,
            message=message,
            occurred_at=self._now(),
            path=volume.mount_point,
            volume_id=volume.id,
            volume_name=volume.name,
            mount_point=volume.mount_point,
            threshold_percent=self.threshold_percent,
            observed_percent=volume.used_percent,
            used_bytes=volume.used_bytes,
            total_bytes=volume.total_bytes,
            available_bytes=volume.available_bytes,
            related_event_ids=[],
        )

    def evaluate(self, volumes: VolumesResponse) -> list[CapacityPressureAlert]:
        """Evaluate exactly the supplied sample; never recollect or infer recovery."""

        with self._lock:
            if volumes.status in {
                CapabilityStatus.ERROR,
                CapabilityStatus.UNAVAILABLE,
                CapabilityStatus.WARMING_UP,
            }:
                # Missing/error samples do not prove recovery, so active IDs stay
                # armed and cannot produce duplicates after a transient failure.
                self._status = volumes.status
                self._message = volumes.message or "Volume capacity could not be evaluated."
                return []

            emitted: list[CapacityPressureAlert] = []
            observations: dict[str, Volume] = {}
            apfs_containers: dict[str, list[Volume]] = {}
            for volume in volumes.items:
                # A zero-total pseudo mount does not provide meaningful pressure.
                if volume.total_bytes <= 0:
                    continue
                if volume.apfs and volume.apfs.container_reference:
                    key = f"apfs:{volume.apfs.container_reference}"
                    apfs_containers.setdefault(key, []).append(volume)
                else:
                    observations[f"volume:{volume.id}"] = volume

            for key, members in apfs_containers.items():
                observations[key] = _apfs_container_observation(members)

            for key, volume in observations.items():
                if volume.used_percent >= self.threshold_percent:
                    if key not in self._above_threshold:
                        alert = self._new_alert(volume)
                        self._alerts.append(alert)
                        emitted.append(alert)
                        self._notify(alert)
                    self._above_threshold.add(key)
                elif volume.used_percent <= self.rearm_percent:
                    # A real recovery below the hysteresis boundary re-arms the
                    # volume. A missing sample or threshold-edge fluctuation does not.
                    self._above_threshold.discard(key)

            self._status = (
                CapabilityStatus.PARTIAL
                if volumes.status == CapabilityStatus.PARTIAL
                or self._configuration_message is not None
                else CapabilityStatus.AVAILABLE
            )
            self._message = self._configuration_message or volumes.message
            return emitted

    def snapshot(
        self, limit: int = MAX_CAPACITY_ALERTS
    ) -> tuple[CapabilityStatus, str | None, list[CapacityPressureAlert]]:
        with self._lock:
            return (
                self._status,
                self._message,
                list(reversed(self._alerts))[: max(0, limit)],
            )

    def get_alert(self, alert_id: str) -> CapacityPressureAlert | None:
        with self._lock:
            return next((alert for alert in self._alerts if alert.id == alert_id), None)
