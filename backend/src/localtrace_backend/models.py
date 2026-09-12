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


class EventsResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    watched_path: str
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


class AlertsResponse(APIModel):
    sampled_at: datetime
    status: CapabilityStatus
    source: str
    message: str | None = None
    watched_path: str
    threshold_bytes: int = Field(gt=0)
    capacity_threshold_percent: float = Field(default=90, gt=0, le=100)
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
