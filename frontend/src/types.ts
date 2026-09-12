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

export interface EventsSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  watched_path: string;
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

export interface AlertsSnapshot {
  sampled_at: string;
  status: CollectorStatus;
  source: string;
  message: string | null;
  watched_path: string;
  threshold_bytes: number;
  capacity_threshold_percent: number;
  items: StorageAlert[];
}

export interface DashboardSnapshot {
  sampled_at: string;
  overall_status: CollectorStatus;
  volumes: VolumeSnapshot;
  quotas: QuotasSnapshot;
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
