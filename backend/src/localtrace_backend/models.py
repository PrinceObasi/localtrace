"""Stable, frontend-facing API models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    """Base model that rejects accidental API-shape changes."""

    model_config = ConfigDict(extra="forbid")


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    WARMING_UP = "warming_up"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class FilesystemFamily(str, Enum):
    APFS = "apfs"
    NFS = "nfs"
    OTHER = "other"


class VolumeHealth(APIModel):
    status: CapabilityStatus
    smart_status: str | None = None
    message: str | None = None


class APFSDetails(APIModel):
    container_reference: str | None = None
    volume_uuid: str | None = None
    roles: list[str] = Field(default_factory=list)
    encrypted: bool | None = None


class NFSDetails(APIModel):
    """Metadata visible to the client for a mounted NFS export.

    ``pnfs_status`` is deliberately independent of the NFS protocol version:
    NFSv4 or NFSv4.1 by itself is not proof that a pNFS layout is in use.
    """

    server: str | None = None
    export: str | None = None
    protocol_version: str | None = None
    mount_options: list[str] = Field(default_factory=list)
    status_flags: list[Literal["dead", "not responding", "recovery"]] = Field(
        default_factory=list
    )
    pnfs_status: CapabilityStatus
    pnfs_message: str


class Volume(APIModel):
    id: str
    name: str
    mount_point: str
    device: str
    filesystem: str
    filesystem_family: FilesystemFamily
    remote: bool
    read_only: bool
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    used_percent: float = Field(ge=0, le=100)
    health: VolumeHealth
    apfs: APFSDetails | None = None
    nfs: NFSDetails | None = None


class VolumesResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    items: list[Volume] = Field(default_factory=list)


class IORate(APIModel):
    read_bytes_per_second: float = Field(ge=0)
    write_bytes_per_second: float = Field(ge=0)
    read_gigabytes_per_second: float = Field(ge=0)
    write_gigabytes_per_second: float = Field(ge=0)


class IODeviceRate(IORate):
    name: str


class IOResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    interval_seconds: float = Field(ge=0)
    aggregate: IORate
    devices: list[IODeviceRate] = Field(default_factory=list)


class FileEventKind(str, Enum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"


class FileEvent(APIModel):
    id: str
    observed_at: datetime
    path: str
    destination_path: str | None = None
    kind: FileEventKind
    owner_uid: int | None = Field(
        default=None,
        description="File owner UID when observable; this does not identify the writer.",
    )
    owner_name: str | None = Field(
        default=None,
        description="File owner account when resolvable; this does not identify the writer.",
    )
    size_before: int | None = Field(default=None, ge=0)
    size_after: int | None = Field(default=None, ge=0)
    delta_bytes: int | None = None
    source: str


class WatchTargetRole(str, Enum):
    DEMO = "demo"
    MODEL_DIRECTORY = "model_directory"
    CONFIGURED = "configured"


class WatchTargetStatus(str, Enum):
    WATCHING = "watching"
    SKIPPED = "skipped"
    ERROR = "error"


class WatchTarget(APIModel):
    """One directory LocalTrace was asked to observe and what happened to it.

    ``skipped`` is a normal state for a well-known model directory that does
    not exist on this Mac. It is not an error and does not degrade the
    capability; the message says why the target is not being watched.
    """

    path: str
    role: WatchTargetRole
    status: WatchTargetStatus
    message: str | None = None


class EventsResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    watched_path: str
    watch_targets: list[WatchTarget] = Field(default_factory=list)
    items: list[FileEvent] = Field(default_factory=list)


class AlertBase(APIModel):
    id: str
    severity: Literal["warning"] = "warning"
    title: str
    message: str
    occurred_at: datetime


class RapidFileGrowthAlert(AlertBase):
    rule: Literal["RAPID_FILE_GROWTH"] = "RAPID_FILE_GROWTH"
    path: str
    threshold_bytes: int = Field(gt=0)
    observed_growth_bytes: int = Field(ge=0)
    related_event_ids: list[str] = Field(default_factory=list)


class CapacityPressureAlert(AlertBase):
    rule: Literal["CAPACITY_PRESSURE"] = "CAPACITY_PRESSURE"
    path: str
    volume_id: str
    volume_name: str
    mount_point: str
    threshold_percent: float = Field(gt=0, le=100)
    observed_percent: float = Field(ge=0, le=100)
    used_bytes: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    related_event_ids: list[str] = Field(default_factory=list, max_length=0)


Alert = Annotated[
    RapidFileGrowthAlert | CapacityPressureAlert,
    Field(discriminator="rule"),
]


class AlertSinkStatus(APIModel):
    """One delivery sink: its static availability overlaid with the outcome
    of its most recent delivery attempt and running success/failure counts."""

    name: str
    status: CapabilityStatus
    target: str | None = None
    message: str | None = None
    delivered_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    last_error: str | None = None


class AlertDeliveryStatus(APIModel):
    """Where new alerts are sent beyond the dashboard, and whether it worked."""

    status: CapabilityStatus
    source: str
    message: str | None = None
    delivered_count: int = Field(ge=0)
    partially_delivered_count: int = Field(default=0, ge=0)
    failed_count: int = Field(ge=0)
    pending_count: int = Field(default=0, ge=0)
    last_delivered_at: datetime | None = None
    sinks: list[AlertSinkStatus] = Field(default_factory=list)


class AlertsResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    watched_path: str
    watch_targets: list[WatchTarget] = Field(default_factory=list)
    threshold_bytes: int = Field(gt=0)
    capacity_threshold_percent: float = Field(default=90, gt=0, le=100)
    delivery: AlertDeliveryStatus | None = None
    items: list[Alert] = Field(default_factory=list)


class QuotaEntry(APIModel):
    """A native current-user quota with at least one non-zero limit."""

    user: str
    uid: int | None = Field(default=None, ge=0)
    filesystem: str
    filesystem_family: FilesystemFamily
    limit_semantics: Literal[
        "absolute_limit", "remaining_availability", "unknown"
    ]
    mount_point: str | None = None
    used_bytes: int = Field(ge=0)
    soft_limit_bytes: int | None = Field(default=None, gt=0)
    hard_limit_bytes: int | None = Field(default=None, gt=0)
    files_used: int = Field(ge=0)
    file_soft_limit: int | None = Field(default=None, gt=0)
    file_hard_limit: int | None = Field(default=None, gt=0)
    block_over_limit: bool
    file_over_limit: bool
    block_grace: str | None = None
    file_grace: str | None = None


class QuotasResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    user: str
    uid: int | None = Field(default=None, ge=0)
    items: list[QuotaEntry] = Field(default_factory=list)


class CapabilitySummary(APIModel):
    status: CapabilityStatus
    source: str
    message: str | None = None


class ServiceStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"


class UsageTargetStatus(str, Enum):
    SCANNED = "scanned"
    PARTIAL = "partial"
    ERROR = "error"


class UsageFile(APIModel):
    path: str
    apparent_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)


class OwnerUsage(APIModel):
    """Bytes grouped by the owning uid of files in watched directories.

    Ownership at scan time is evidence for investigation; it does not prove
    which process or person wrote the files.
    """

    uid: int = Field(ge=0)
    owner_name: str | None = None
    file_count: int = Field(ge=0)
    apparent_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)
    share_percent: float = Field(ge=0, le=100)
    top_files: list[UsageFile] = Field(default_factory=list)


class UsageTarget(APIModel):
    path: str
    status: UsageTargetStatus
    file_count: int = Field(ge=0)
    apparent_bytes: int = Field(ge=0)
    allocated_bytes: int = Field(ge=0)
    message: str | None = None


class UsageResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    scan_started_at: datetime | None = None
    scan_duration_seconds: float = Field(ge=0)
    scan_interval_seconds: float = Field(gt=0)
    truncated: bool = False
    file_count: int = Field(ge=0)
    total_apparent_bytes: int = Field(ge=0)
    total_allocated_bytes: int = Field(ge=0)
    owner_count: int = Field(default=0, ge=0)
    directories: list[UsageTarget] = Field(default_factory=list)
    owners: list[OwnerUsage] = Field(default_factory=list)


class ProbeStatus(APIModel):
    status: CapabilityStatus
    source: str
    message: str | None = None


class APFSContainerVolume(APIModel):
    device: str
    name: str | None = None
    roles: list[str] = Field(default_factory=list)
    capacity_in_use_bytes: int | None = Field(default=None, ge=0)
    capacity_quota_bytes: int | None = Field(default=None, ge=0)
    capacity_reserve_bytes: int | None = Field(default=None, ge=0)
    filevault: bool | None = None
    locked: bool | None = None
    sealed: str | None = None


class APFSContainer(APIModel):
    """One APFS container from ``diskutil apfs list``.

    ``capacity_quota_bytes`` on a member volume is a native APFS limit set at
    volume creation; it is the only per-volume quota mechanism APFS has and is
    distinct from per-user quotas.
    """

    reference: str
    uuid: str | None = None
    capacity_ceiling_bytes: int = Field(ge=0)
    capacity_free_bytes: int = Field(ge=0)
    used_percent: float = Field(ge=0, le=100)
    fusion: bool | None = None
    physical_stores: list[str] = Field(default_factory=list)
    volumes: list[APFSContainerVolume] = Field(default_factory=list)


class APFSContainersProbe(ProbeStatus):
    items: list[APFSContainer] = Field(default_factory=list)


class SnapshotGroup(APIModel):
    mount_point: str
    volume_group: str | None = None
    count: int = Field(ge=0)
    oldest: datetime | None = None
    newest: datetime | None = None
    recent_names: list[str] = Field(default_factory=list)


class SnapshotsProbe(ProbeStatus):
    items: list[SnapshotGroup] = Field(default_factory=list)


class NVMeDevice(APIModel):
    name: str
    bsd_name: str | None = None
    model: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    health: VolumeHealth
    trim_support: bool | None = None
    removable: bool | None = None
    link_speed: str | None = None
    link_width: str | None = None


class NVMeProbe(ProbeStatus):
    items: list[NVMeDevice] = Field(default_factory=list)


class StorageHealthResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    refreshed_at: datetime | None = None
    refresh_interval_seconds: float = Field(gt=0)
    apfs: APFSContainersProbe
    snapshots: SnapshotsProbe
    nvme: NVMeProbe


class BenchmarkStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BenchmarkPhase(str, Enum):
    QUEUED = "queued"
    WRITE = "write"
    READ = "read"
    DONE = "done"


class BenchmarkRequest(APIModel):
    """Where to measure. Give a mount point, or a directory for finer control."""

    mount_point: str | None = None
    directory: str | None = None
    size_bytes: int | None = Field(default=None, gt=0)
    block_bytes: int | None = Field(default=None, gt=0)


class BenchmarkPhaseResult(APIModel):
    bytes: int = Field(ge=0)
    seconds: float = Field(ge=0)
    flush_seconds: float | None = Field(default=None, ge=0)
    bytes_per_second: float = Field(ge=0)
    gb_per_second: float = Field(ge=0)


class BenchmarkJob(APIModel):
    """One on-demand throughput measurement of a specific mount.

    ``cache_bypass`` and ``flush_method`` record what the run could actually
    control, so a reader knows whether the read phase touched the buffer
    cache and whether the write phase ended on stable storage.
    """

    id: str
    status: BenchmarkStatus
    phase: BenchmarkPhase
    progress_percent: float = Field(ge=0, le=100)
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    directory: str
    mount_point: str | None = None
    filesystem: str | None = None
    filesystem_family: str | None = None
    remote: bool | None = None
    size_bytes: int = Field(gt=0)
    block_bytes: int = Field(gt=0)
    cache_bypass: bool = False
    flush_method: str
    write: BenchmarkPhaseResult | None = None
    read: BenchmarkPhaseResult | None = None
    message: str | None = None


class BenchmarksResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    running: bool = False
    max_size_bytes: int = Field(gt=0)
    default_size_bytes: int = Field(gt=0)
    items: list[BenchmarkJob] = Field(default_factory=list)


class HealthResponse(APIModel):
    service: str
    version: str
    status: ServiceStatus
    sampled_at: datetime
    platform: str
    capabilities: dict[str, CapabilitySummary]


class DashboardResponse(APIModel):
    sampled_at: datetime
    overall_status: CapabilityStatus
    volumes: VolumesResponse
    io: IOResponse
    events: EventsResponse
    alerts: AlertsResponse
    quotas: QuotasResponse
    usage: UsageResponse
    storage_health: StorageHealthResponse
    benchmarks: BenchmarksResponse
