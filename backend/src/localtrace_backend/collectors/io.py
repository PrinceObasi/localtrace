"""Stateful whole-disk I/O rate sampling using cumulative OS counters."""

from __future__ import annotations

import platform
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psutil

from localtrace_backend.models import (
    CapabilityStatus,
    IODeviceRate,
    IORate,
    IOResponse,
)


MIN_SAMPLE_INTERVAL_SECONDS = 0.05
SOURCE = "psutil.disk_io_counters(perdisk=True)"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _known_whole_disk(name: str, system: str) -> bool:
    lowered = name.lower()
    if system == "Darwin":
        return re.fullmatch(r"disk\d+", lowered) is not None
    if system == "Linux":
        return (
            re.fullmatch(
                r"(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)",
                lowered,
            )
            is not None
        )
    if system == "Windows":
        return re.fullmatch(r"physicaldrive\d+", lowered) is not None
    return False


def select_whole_disks(
    counters: Mapping[str, Any], system: str
) -> tuple[dict[str, Any], bool]:
    """Select likely whole physical devices and avoid partition double-counting.

    The boolean indicates whether the OS naming convention was recognized. A
    conservative fallback is still returned for unknown platforms, but callers
    mark it partial rather than claiming certainty.
    """

    known = {name: value for name, value in counters.items() if _known_whole_disk(name, system)}
    if known:
        return known, True

    pseudo_prefixes = ("loop", "ram", "zram", "fd", "sr", "dm-")
    fallback = {
        name: value
        for name, value in counters.items()
        if not name.lower().startswith(pseudo_prefixes)
    }
    return fallback, False


@dataclass(frozen=True)
class _Counter:
    read_bytes: int
    write_bytes: int


@dataclass(frozen=True)
class _Snapshot:
    monotonic_time: float
    counters: dict[str, _Counter]


def _zero_rate() -> IORate:
    return IORate(
        read_bytes_per_second=0,
        write_bytes_per_second=0,
        read_gigabytes_per_second=0,
        write_gigabytes_per_second=0,
    )


def _rate(name: str, read_bps: float, write_bps: float) -> IODeviceRate:
    read_bps = max(0.0, read_bps)
    write_bps = max(0.0, write_bps)
    return IODeviceRate(
        name=name,
        read_bytes_per_second=round(read_bps, 3),
        write_bytes_per_second=round(write_bps, 3),
        read_gigabytes_per_second=round(read_bps / 1_000_000_000, 9),
        write_gigabytes_per_second=round(write_bps / 1_000_000_000, 9),
    )


class DiskIOSampler:
    """Convert cumulative whole-disk byte counters into rates.

    The first observation establishes a baseline and is explicitly returned as
    ``warming_up``. Calls are serialized because the previous snapshot is
    shared process state.
    """

    def __init__(
        self,
        *,
        counters_provider: Callable[..., Mapping[str, Any] | None] = psutil.disk_io_counters,
        system_provider: Callable[[], str] = platform.system,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
        min_interval_seconds: float = MIN_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self._counters_provider = counters_provider
        self._system_provider = system_provider
        self._monotonic = monotonic
        self._now = now
        self._min_interval_seconds = min_interval_seconds
        self._previous: _Snapshot | None = None
        self._lock = threading.Lock()

    def _response(
        self,
        *,
        status: CapabilityStatus,
        sampled_at: datetime,
        message: str | None,
        interval: float = 0,
        devices: list[IODeviceRate] | None = None,
    ) -> IOResponse:
        device_rates = devices or []
        total_read = sum(item.read_bytes_per_second for item in device_rates)
        total_write = sum(item.write_bytes_per_second for item in device_rates)
        return IOResponse(
            sampled_at=sampled_at,
            status=status,
            source=SOURCE,
            message=message,
            interval_seconds=round(max(0.0, interval), 6),
            aggregate=IORate(
                read_bytes_per_second=round(total_read, 3),
                write_bytes_per_second=round(total_write, 3),
                read_gigabytes_per_second=round(total_read / 1_000_000_000, 9),
                write_gigabytes_per_second=round(total_write / 1_000_000_000, 9),
            )
            if device_rates
            else _zero_rate(),
            devices=device_rates,
        )

    def sample(self) -> IOResponse:
        with self._lock:
            sampled_at = self._now()
            try:
                raw = self._counters_provider(perdisk=True, nowrap=True)
            except (OSError, psutil.Error) as exc:
                return self._response(
                    status=CapabilityStatus.ERROR,
                    sampled_at=sampled_at,
                    message=f"Disk I/O counters could not be read: {exc}",
                )

            if not raw:
                self._previous = None
                return self._response(
                    status=CapabilityStatus.UNAVAILABLE,
                    sampled_at=sampled_at,
                    message="The operating system exposed no disk I/O counters.",
                )

            selected, recognized = select_whole_disks(raw, self._system_provider())
            if not selected:
                self._previous = None
                return self._response(
                    status=CapabilityStatus.UNAVAILABLE,
                    sampled_at=sampled_at,
                    message="No whole-disk counters could be identified.",
                )

            current_time = self._monotonic()
            current = {
                name: _Counter(
                    read_bytes=max(0, int(getattr(value, "read_bytes", 0))),
                    write_bytes=max(0, int(getattr(value, "write_bytes", 0))),
                )
                for name, value in selected.items()
            }
            snapshot = _Snapshot(current_time, current)
            if self._previous is None:
                self._previous = snapshot
                return self._response(
                    status=CapabilityStatus.WARMING_UP,
                    sampled_at=sampled_at,
                    message="A baseline was captured; take a second sample to calculate rates.",
                )

            interval = current_time - self._previous.monotonic_time
            if interval < self._min_interval_seconds:
                return self._response(
                    status=CapabilityStatus.WARMING_UP,
                    sampled_at=sampled_at,
                    message=(
                        f"Collecting at least {self._min_interval_seconds:g} seconds "
                        "of counter history."
                    ),
                    interval=max(0.0, interval),
                )

            common_names = sorted(current.keys() & self._previous.counters.keys())
            if not common_names:
                self._previous = snapshot
                return self._response(
                    status=CapabilityStatus.WARMING_UP,
                    sampled_at=sampled_at,
                    message="The device set changed; a new baseline was captured.",
                    interval=interval,
                )

            counter_reset = False
            rates: list[IODeviceRate] = []
            for name in common_names:
                before = self._previous.counters[name]
                after = current[name]
                read_delta = after.read_bytes - before.read_bytes
                write_delta = after.write_bytes - before.write_bytes
                if read_delta < 0 or write_delta < 0:
                    counter_reset = True
                rates.append(
                    _rate(
                        name,
                        max(0, read_delta) / interval,
                        max(0, write_delta) / interval,
                    )
                )

            device_set_changed = current.keys() != self._previous.counters.keys()
            self._previous = snapshot
            messages: list[str] = []
            status = CapabilityStatus.AVAILABLE
            if not recognized:
                status = CapabilityStatus.PARTIAL
                messages.append(
                    "Whole-device naming was not recognized; rates use non-pseudo OS counters."
                )
            if device_set_changed:
                status = CapabilityStatus.PARTIAL
                messages.append("New or removed devices need one complete sampling interval.")
            if counter_reset:
                status = CapabilityStatus.PARTIAL
                messages.append("A counter reset was observed; negative deltas were ignored.")

            return self._response(
                status=status,
                sampled_at=sampled_at,
                message=" ".join(messages) or None,
                interval=interval,
                devices=rates,
            )
