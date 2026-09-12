export type CollectorStatus =
  | "available"
  | "partial"
  | "warming_up"
  | "unavailable"
  | "error";

export interface HealthStatus {
  status: CollectorStatus;
  smart_status: string | null;
  message: string | null;
}

export interface ApfsDetails {
  container_reference: string | null;
  volume_uuid: string | null;
  roles: string[];
  encrypted: boolean | null;
}

export type NfsStatusFlag = "dead" | "not responding" | "recovery";

export interface NfsDetails {
  server: string | null;
  export: string | null;
  protocol_version: string | null;
  mount_options: string[];
  status_flags: NfsStatusFlag[];
  pnfs_status: CollectorStatus;
  pnfs_message: string;
}

export interface Volume {
  id: string;
  name: string;
  mount_point: string;
  device: string;
  filesystem: string;
  filesystem_family: "apfs" | "nfs" | "other";
  remote: boolean;
  read_only: boolean;
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  used_percent: number;
  health: HealthStatus;
  apfs: ApfsDetails | null;
  nfs: NfsDetails | null;
}

export interface VolumeSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  items: Volume[];
}

export interface IoAggregate {
  read_bytes_per_second: number;
  write_bytes_per_second: number;
  read_gigabytes_per_second: number;
  write_gigabytes_per_second: number;
}

export interface IoDevice extends IoAggregate {
  name: string;
}

export interface IoSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  interval_seconds: number;
  aggregate: IoAggregate;
  devices: IoDevice[];
}

export type FileEventKind = "created" | "modified" | "deleted" | "moved";

export interface FileEvent {
  id: string;
  observed_at: string;
  path: string;
  destination_path: string | null;
  kind: FileEventKind;
  owner_uid: number | null;
  owner_name: string | null;
  size_before: number | null;
  size_after: number | null;
  delta_bytes: number | null;
  source: string;
}

export type WatchTargetRole = "demo" | "model_directory" | "configured";
export type WatchTargetStatus = "watching" | "skipped" | "error";

export interface WatchTarget {
  path: string;
  role: WatchTargetRole;
  status: WatchTargetStatus;
  message: string | null;
}

export interface EventsSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  watched_path: string;
  watch_targets: WatchTarget[];
  items: FileEvent[];
}

export interface QuotaEntry {
  user: string;
  uid: number | null;
  filesystem: string;
  filesystem_family: "apfs" | "nfs" | "other";
  mount_point: string | null;
  used_bytes: number;
  soft_limit_bytes: number | null;
  hard_limit_bytes: number | null;
  files_used: number;
  file_soft_limit: number | null;
  file_hard_limit: number | null;
  limit_semantics: "absolute_limit" | "remaining_availability" | "unknown";
  block_over_limit: boolean;
  file_over_limit: boolean;
  block_grace: string | null;
  file_grace: string | null;
}

export interface QuotasSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  user: string;
  uid: number | null;
  items: QuotaEntry[];
}

export type AlertSeverity = "warning";
export type AlertRule = "RAPID_FILE_GROWTH" | "CAPACITY_PRESSURE";

interface BaseStorageAlert {
  id: string;
  severity: AlertSeverity;
  title: string;
  message: string;
  occurred_at: string;
  related_event_ids: string[];
}

export interface RapidFileGrowthAlert extends BaseStorageAlert {
  rule: "RAPID_FILE_GROWTH";
  path: string;
  threshold_bytes: number;
  observed_growth_bytes: number;
}

export interface CapacityPressureAlert extends BaseStorageAlert {
  rule: "CAPACITY_PRESSURE";
  path: string;
  volume_id: string;
  volume_name: string;
  mount_point: string;
  threshold_percent: number;
  observed_percent: number;
  used_bytes: number;
  available_bytes: number;
  total_bytes: number;
}

export type StorageAlert = RapidFileGrowthAlert | CapacityPressureAlert;

export interface AlertSinkStatus {
  name: string;
  status: CollectorStatus;
  target: string | null;
  message: string | null;
}

export interface AlertDeliveryStatus {
  status: CollectorStatus;
  source: string;
  message: string | null;
  delivered_count: number;
  failed_count: number;
  last_delivered_at: string | null;
  sinks: AlertSinkStatus[];
}

export interface AlertsSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  watched_path: string;
  watch_targets: WatchTarget[];
  threshold_bytes: number;
  capacity_threshold_percent: number;
  delivery: AlertDeliveryStatus | null;
  items: StorageAlert[];
}

export type UsageTargetStatus = "scanned" | "partial" | "error";

export interface UsageFile {
  path: string;
  apparent_bytes: number;
  allocated_bytes: number;
}

export interface OwnerUsage {
  uid: number;
  owner_name: string | null;
  file_count: number;
  apparent_bytes: number;
  allocated_bytes: number;
  share_percent: number;
  top_files: UsageFile[];
}

export interface UsageTarget {
  path: string;
  status: UsageTargetStatus;
  file_count: number;
  apparent_bytes: number;
  allocated_bytes: number;
  message: string | null;
}

export interface UsageSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  scan_started_at: string | null;
  scan_duration_seconds: number;
  scan_interval_seconds: number;
  truncated: boolean;
  file_count: number;
  total_apparent_bytes: number;
  total_allocated_bytes: number;
  directories: UsageTarget[];
  owners: OwnerUsage[];
}

export interface ProbeStatus {
  status: CollectorStatus;
  source: string;
  message: string | null;
}

export interface ApfsContainerVolume {
  device: string;
  name: string | null;
  roles: string[];
  capacity_in_use_bytes: number | null;
  capacity_quota_bytes: number | null;
  capacity_reserve_bytes: number | null;
  filevault: boolean | null;
  locked: boolean | null;
  sealed: string | null;
}

export interface ApfsContainer {
  reference: string;
  uuid: string | null;
  capacity_ceiling_bytes: number;
  capacity_free_bytes: number;
  used_percent: number;
  fusion: boolean | null;
  physical_stores: string[];
  volumes: ApfsContainerVolume[];
}

export interface SnapshotGroup {
  mount_point: string;
  volume_group: string | null;
  count: number;
  oldest: string | null;
  newest: string | null;
  recent_names: string[];
}

export interface NvmeDevice {
  name: string;
  bsd_name: string | null;
  model: string | null;
  size_bytes: number | null;
  health: HealthStatus;
  trim_support: boolean | null;
  removable: boolean | null;
  link_speed: string | null;
  link_width: string | null;
}

export interface StorageHealthSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  refreshed_at: string | null;
  refresh_interval_seconds: number;
  apfs: ProbeStatus & { items: ApfsContainer[] };
  snapshots: ProbeStatus & { items: SnapshotGroup[] };
  nvme: ProbeStatus & { items: NvmeDevice[] };
}

export interface DashboardSnapshot {
  sampled_at: string;
  overall_status: CollectorStatus;
  volumes: VolumeSnapshot;
  quotas: QuotasSnapshot;
  usage: UsageSnapshot;
  storage_health: StorageHealthSnapshot;
  io: IoSnapshot;
  events: EventsSnapshot;
  alerts: AlertsSnapshot;
}

export interface ThroughputSample {
  sampledAt: string;
  readBytesPerSecond: number;
  writeBytesPerSecond: number;
}

export type ConnectionState = "connecting" | "live" | "offline";
