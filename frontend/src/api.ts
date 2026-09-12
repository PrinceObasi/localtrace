import type {
  AlertDeliveryStatus,
  ApfsContainer,
  ApfsDetails,
  CollectorStatus,
  DashboardSnapshot,
  FileEvent,
  FileEventKind,
  IoAggregate,
  IoDevice,
  NfsDetails,
  NfsStatusFlag,
  OwnerUsage,
  QuotaEntry,
  StorageAlert,
  StorageHealthSnapshot,
  NvmeDevice,
  ProbeStatus,
  SnapshotGroup,
  UsageSnapshot,
  UsageTarget,
  UsageTargetStatus,
  Volume,
  WatchTarget,
  WatchTargetRole,
  WatchTargetStatus,
} from "./types";

const DASHBOARD_ENDPOINT = "/api/v1/dashboard";
const REQUEST_TIMEOUT_MS = 4_000;

type JsonObject = Record<string, unknown>;

const statuses = new Set<CollectorStatus>([
  "available",
  "partial",
  "warming_up",
  "unavailable",
  "error",
]);

const watchTargetRoles = new Set<WatchTargetRole>([
  "demo",
  "model_directory",
  "configured",
]);
const watchTargetStatuses = new Set<WatchTargetStatus>([
  "watching",
  "skipped",
  "error",
]);

function record(value: unknown, path: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Collector response is missing ${path}`);
  }
  return value as JsonObject;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string") {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return value;
}

function nullableString(value: unknown, path: string): string | null {
  if (value === null || value === undefined) return null;
  return string(value, path);
}

function number(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return value;
}

function integer(value: unknown, path: string): number {
  const result = number(value, path);
  if (!Number.isInteger(result)) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return result;
}

function percentage(value: unknown, path: string, positive = false): number {
  const result = number(value, path);
  if (result > 100 || (positive && result === 0)) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return result;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return value;
}

function status(value: unknown, path: string): CollectorStatus {
  if (typeof value !== "string" || !statuses.has(value as CollectorStatus)) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return value as CollectorStatus;
}

function stringList(value: unknown, path: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return value;
}

function nullableBoolean(value: unknown, path: string): boolean | null {
  if (value === null || value === undefined) return null;
  return boolean(value, path);
}

function nullableNumber(
  value: unknown,
  path: string,
  allowNegative = false,
): number | null {
  if (value === null || value === undefined) return null;
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    (!allowNegative && value < 0)
  ) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return value;
}

function nullableInteger(value: unknown, path: string): number | null {
  if (value === null || value === undefined) return null;
  return integer(value, path);
}

function nullablePositiveInteger(value: unknown, path: string): number | null {
  const result = nullableInteger(value, path);
  if (result !== null && result === 0) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return result;
}

function oneOf<T extends string>(
  value: unknown,
  allowed: readonly T[],
  path: string,
): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return value as T;
}

function normalizeRate(value: unknown, path: string): IoAggregate {
  const input = record(value, path);
  return {
    read_bytes_per_second: number(
      input.read_bytes_per_second,
      `${path}.read_bytes_per_second`,
    ),
    write_bytes_per_second: number(
      input.write_bytes_per_second,
      `${path}.write_bytes_per_second`,
    ),
    read_gigabytes_per_second: number(
      input.read_gigabytes_per_second,
      `${path}.read_gigabytes_per_second`,
    ),
    write_gigabytes_per_second: number(
      input.write_gigabytes_per_second,
      `${path}.write_gigabytes_per_second`,
    ),
  };
}

function normalizeDevice(value: unknown, index: number): IoDevice {
  const path = `io.devices[${index}]`;
  const input = record(value, path);
  return {
    name: string(input.name, `${path}.name`),
    ...normalizeRate(input, path),
  };
}

function normalizeApfs(value: unknown, path: string): ApfsDetails | null {
  if (value === null || value === undefined) return null;
  const input = record(value, path);
  return {
    container_reference: nullableString(
      input.container_reference,
      `${path}.container_reference`,
    ),
    volume_uuid: nullableString(input.volume_uuid, `${path}.volume_uuid`),
    roles: stringList(input.roles, `${path}.roles`),
    encrypted: nullableBoolean(input.encrypted, `${path}.encrypted`),
  };
}

function normalizeNfs(value: unknown, path: string): NfsDetails | null {
  if (value === null || value === undefined) return null;
  const input = record(value, path);
  const statusFlags = stringList(input.status_flags, `${path}.status_flags`);
  const allowedStatusFlags: readonly NfsStatusFlag[] = [
    "dead",
    "not responding",
    "recovery",
  ];
  if (
    statusFlags.some(
      (flag) => !allowedStatusFlags.includes(flag as NfsStatusFlag),
    )
  ) {
    throw new Error(`Collector response has invalid ${path}.status_flags`);
  }
  return {
    server: nullableString(input.server, `${path}.server`),
    export: nullableString(input.export, `${path}.export`),
    protocol_version: nullableString(
      input.protocol_version,
      `${path}.protocol_version`,
    ),
    mount_options: stringList(input.mount_options, `${path}.mount_options`),
    status_flags: statusFlags as NfsStatusFlag[],
    pnfs_status: status(input.pnfs_status, `${path}.pnfs_status`),
    pnfs_message: string(input.pnfs_message, `${path}.pnfs_message`),
  };
}

function normalizeVolume(value: unknown, index: number): Volume {
  const path = `volumes.items[${index}]`;
  const input = record(value, path);
  const health = record(input.health, `${path}.health`);
  const family = string(input.filesystem_family, `${path}.filesystem_family`);
  if (family !== "apfs" && family !== "nfs" && family !== "other") {
    throw new Error(`Collector response has invalid ${path}.filesystem_family`);
  }

  const usedPercent = number(input.used_percent, `${path}.used_percent`);
  if (usedPercent > 100) {
    throw new Error(`Collector response has invalid ${path}.used_percent`);
  }

  return {
    id: string(input.id, `${path}.id`),
    name: string(input.name, `${path}.name`),
    mount_point: string(input.mount_point, `${path}.mount_point`),
    device: string(input.device, `${path}.device`),
    filesystem: string(input.filesystem, `${path}.filesystem`),
    filesystem_family: family,
    remote: boolean(input.remote, `${path}.remote`),
    read_only: boolean(input.read_only, `${path}.read_only`),
    total_bytes: integer(input.total_bytes, `${path}.total_bytes`),
    used_bytes: integer(input.used_bytes, `${path}.used_bytes`),
    available_bytes: integer(
      input.available_bytes,
      `${path}.available_bytes`,
    ),
    used_percent: usedPercent,
    health: {
      status: status(health.status, `${path}.health.status`),
      smart_status: nullableString(
        health.smart_status,
        `${path}.health.smart_status`,
      ),
      message: nullableString(health.message, `${path}.health.message`),
    },
    apfs: normalizeApfs(input.apfs, `${path}.apfs`),
    nfs: normalizeNfs(input.nfs, `${path}.nfs`),
  };
}

function normalizeQuota(value: unknown, index: number): QuotaEntry {
  const path = `quotas.items[${index}]`;
  const input = record(value, path);
  const family = string(input.filesystem_family, `${path}.filesystem_family`);
  if (family !== "apfs" && family !== "nfs" && family !== "other") {
    throw new Error(`Collector response has invalid ${path}.filesystem_family`);
  }
  return {
    user: string(input.user, `${path}.user`),
    uid: nullableInteger(input.uid, `${path}.uid`),
    filesystem: string(input.filesystem, `${path}.filesystem`),
    filesystem_family: family,
    mount_point: nullableString(input.mount_point, `${path}.mount_point`),
    used_bytes: integer(input.used_bytes, `${path}.used_bytes`),
    soft_limit_bytes: nullablePositiveInteger(
      input.soft_limit_bytes,
      `${path}.soft_limit_bytes`,
    ),
    hard_limit_bytes: nullablePositiveInteger(
      input.hard_limit_bytes,
      `${path}.hard_limit_bytes`,
    ),
    files_used: integer(input.files_used, `${path}.files_used`),
    file_soft_limit: nullablePositiveInteger(
      input.file_soft_limit,
      `${path}.file_soft_limit`,
    ),
    file_hard_limit: nullablePositiveInteger(
      input.file_hard_limit,
      `${path}.file_hard_limit`,
    ),
    limit_semantics: oneOf(
      input.limit_semantics,
      ["absolute_limit", "remaining_availability", "unknown"] as const,
      `${path}.limit_semantics`,
    ),
    block_over_limit: boolean(
      input.block_over_limit,
      `${path}.block_over_limit`,
    ),
    file_over_limit: boolean(
      input.file_over_limit,
      `${path}.file_over_limit`,
    ),
    block_grace: nullableString(input.block_grace, `${path}.block_grace`),
    file_grace: nullableString(input.file_grace, `${path}.file_grace`),
  };
}

const usageTargetStatuses = new Set<UsageTargetStatus>([
  "scanned",
  "partial",
  "error",
]);

function normalizeUsage(value: unknown): UsageSnapshot {
  const usage = record(value, "usage");
  if (!Array.isArray(usage.directories) || !Array.isArray(usage.owners)) {
    throw new Error("Collector response has invalid usage lists");
  }
  const directories: UsageTarget[] = usage.directories.map((item, index) => {
    const path = `usage.directories[${index}]`;
    const target = record(item, path);
    const targetStatus = string(target.status, `${path}.status`);
    if (!usageTargetStatuses.has(targetStatus as UsageTargetStatus)) {
      throw new Error(`Collector response has invalid ${path}.status`);
    }
    return {
      path: string(target.path, `${path}.path`),
      status: targetStatus as UsageTargetStatus,
      file_count: integer(target.file_count, `${path}.file_count`),
      apparent_bytes: integer(target.apparent_bytes, `${path}.apparent_bytes`),
      allocated_bytes: integer(target.allocated_bytes, `${path}.allocated_bytes`),
      message: nullableString(target.message, `${path}.message`),
    };
  });
  const owners: OwnerUsage[] = usage.owners.map((item, index) => {
    const path = `usage.owners[${index}]`;
    const owner = record(item, path);
    if (!Array.isArray(owner.top_files)) {
      throw new Error(`Collector response has invalid ${path}.top_files`);
    }
    return {
      uid: integer(owner.uid, `${path}.uid`),
      owner_name: nullableString(owner.owner_name, `${path}.owner_name`),
      file_count: integer(owner.file_count, `${path}.file_count`),
      apparent_bytes: integer(owner.apparent_bytes, `${path}.apparent_bytes`),
      allocated_bytes: integer(owner.allocated_bytes, `${path}.allocated_bytes`),
      share_percent: percentage(owner.share_percent, `${path}.share_percent`),
      top_files: owner.top_files.map((file, fileIndex) => {
        const filePath = `${path}.top_files[${fileIndex}]`;
        const entry = record(file, filePath);
        return {
          path: string(entry.path, `${filePath}.path`),
          apparent_bytes: integer(entry.apparent_bytes, `${filePath}.apparent_bytes`),
          allocated_bytes: integer(entry.allocated_bytes, `${filePath}.allocated_bytes`),
        };
      }),
    };
  });
  return {
    sampled_at: string(usage.sampled_at, "usage.sampled_at"),
    status: status(usage.status, "usage.status"),
    source: string(usage.source, "usage.source"),
    message: nullableString(usage.message, "usage.message"),
    scan_started_at: nullableString(usage.scan_started_at, "usage.scan_started_at"),
    scan_duration_seconds: number(usage.scan_duration_seconds, "usage.scan_duration_seconds"),
    scan_interval_seconds: number(usage.scan_interval_seconds, "usage.scan_interval_seconds"),
    truncated: boolean(usage.truncated, "usage.truncated"),
    file_count: integer(usage.file_count, "usage.file_count"),
    total_apparent_bytes: integer(usage.total_apparent_bytes, "usage.total_apparent_bytes"),
    total_allocated_bytes: integer(usage.total_allocated_bytes, "usage.total_allocated_bytes"),
    directories,
    owners,
  };
}

function normalizeDelivery(value: unknown): AlertDeliveryStatus | null {
  if (value === undefined || value === null) return null;
  const delivery = record(value, "alerts.delivery");
  if (!Array.isArray(delivery.sinks)) {
    throw new Error("Collector response has invalid alerts.delivery.sinks");
  }
  return {
    status: status(delivery.status, "alerts.delivery.status"),
    source: string(delivery.source, "alerts.delivery.source"),
    message: nullableString(delivery.message, "alerts.delivery.message"),
    delivered_count: integer(delivery.delivered_count, "alerts.delivery.delivered_count"),
    failed_count: integer(delivery.failed_count, "alerts.delivery.failed_count"),
    last_delivered_at: nullableString(
      delivery.last_delivered_at,
      "alerts.delivery.last_delivered_at",
    ),
    sinks: delivery.sinks.map((item, index) => {
      const path = `alerts.delivery.sinks[${index}]`;
      const sink = record(item, path);
      return {
        name: string(sink.name, `${path}.name`),
        status: status(sink.status, `${path}.status`),
        target: nullableString(sink.target, `${path}.target`),
        message: nullableString(sink.message, `${path}.message`),
      };
    }),
  };
}

function normalizeProbe(value: unknown, path: string): ProbeStatus & { items: unknown[] } {
  const probe = record(value, path);
  if (!Array.isArray(probe.items)) {
    throw new Error(`Collector response has invalid ${path}.items`);
  }
  return {
    status: status(probe.status, `${path}.status`),
    source: string(probe.source, `${path}.source`),
    message: nullableString(probe.message, `${path}.message`),
    items: probe.items,
  };
}

function normalizeApfsContainer(value: unknown, path: string): ApfsContainer {
  const input = record(value, path);
  if (!Array.isArray(input.volumes)) {
    throw new Error(`Collector response has invalid ${path}.volumes`);
  }
  return {
    reference: string(input.reference, `${path}.reference`),
    uuid: nullableString(input.uuid, `${path}.uuid`),
    capacity_ceiling_bytes: integer(input.capacity_ceiling_bytes, `${path}.capacity_ceiling_bytes`),
    capacity_free_bytes: integer(input.capacity_free_bytes, `${path}.capacity_free_bytes`),
    used_percent: percentage(input.used_percent, `${path}.used_percent`),
    fusion: nullableBoolean(input.fusion, `${path}.fusion`),
    physical_stores: stringList(input.physical_stores, `${path}.physical_stores`),
    volumes: input.volumes.map((item, index) => {
      const volumePath = `${path}.volumes[${index}]`;
      const volume = record(item, volumePath);
      return {
        device: string(volume.device, `${volumePath}.device`),
        name: nullableString(volume.name, `${volumePath}.name`),
        roles: stringList(volume.roles, `${volumePath}.roles`),
        capacity_in_use_bytes: nullableInteger(volume.capacity_in_use_bytes, `${volumePath}.capacity_in_use_bytes`),
        capacity_quota_bytes: nullableInteger(volume.capacity_quota_bytes, `${volumePath}.capacity_quota_bytes`),
        capacity_reserve_bytes: nullableInteger(volume.capacity_reserve_bytes, `${volumePath}.capacity_reserve_bytes`),
        filevault: nullableBoolean(volume.filevault, `${volumePath}.filevault`),
        locked: nullableBoolean(volume.locked, `${volumePath}.locked`),
        sealed: nullableString(volume.sealed, `${volumePath}.sealed`),
      };
    }),
  };
}

function normalizeSnapshotGroup(value: unknown, path: string): SnapshotGroup {
  const input = record(value, path);
  return {
    mount_point: string(input.mount_point, `${path}.mount_point`),
    volume_group: nullableString(input.volume_group, `${path}.volume_group`),
    count: integer(input.count, `${path}.count`),
    oldest: nullableString(input.oldest, `${path}.oldest`),
    newest: nullableString(input.newest, `${path}.newest`),
    recent_names: stringList(input.recent_names, `${path}.recent_names`),
  };
}

function normalizeNvmeDevice(value: unknown, path: string): NvmeDevice {
  const input = record(value, path);
  const health = record(input.health, `${path}.health`);
  return {
    name: string(input.name, `${path}.name`),
    bsd_name: nullableString(input.bsd_name, `${path}.bsd_name`),
    model: nullableString(input.model, `${path}.model`),
    size_bytes: nullableInteger(input.size_bytes, `${path}.size_bytes`),
    health: {
      status: status(health.status, `${path}.health.status`),
      smart_status: nullableString(health.smart_status, `${path}.health.smart_status`),
      message: nullableString(health.message, `${path}.health.message`),
    },
    trim_support: nullableBoolean(input.trim_support, `${path}.trim_support`),
    removable: nullableBoolean(input.removable, `${path}.removable`),
    link_speed: nullableString(input.link_speed, `${path}.link_speed`),
    link_width: nullableString(input.link_width, `${path}.link_width`),
  };
}

function normalizeStorageHealth(value: unknown): StorageHealthSnapshot {
  const input = record(value, "storage_health");
  const apfs = normalizeProbe(input.apfs, "storage_health.apfs");
  const snapshots = normalizeProbe(input.snapshots, "storage_health.snapshots");
  const nvme = normalizeProbe(input.nvme, "storage_health.nvme");
  return {
    sampled_at: string(input.sampled_at, "storage_health.sampled_at"),
    status: status(input.status, "storage_health.status"),
    source: string(input.source, "storage_health.source"),
    message: nullableString(input.message, "storage_health.message"),
    refreshed_at: nullableString(input.refreshed_at, "storage_health.refreshed_at"),
    refresh_interval_seconds: number(input.refresh_interval_seconds, "storage_health.refresh_interval_seconds"),
    apfs: {
      ...apfs,
      items: apfs.items.map((item, index) => normalizeApfsContainer(item, `storage_health.apfs.items[${index}]`)),
    },
    snapshots: {
      ...snapshots,
      items: snapshots.items.map((item, index) => normalizeSnapshotGroup(item, `storage_health.snapshots.items[${index}]`)),
    },
    nvme: {
      ...nvme,
      items: nvme.items.map((item, index) => normalizeNvmeDevice(item, `storage_health.nvme.items[${index}]`)),
    },
  };
}

function normalizeWatchTargets(value: unknown, path: string): WatchTarget[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) {
    throw new Error(`Collector response has invalid ${path}`);
  }
  return value.map((item, index) => {
    const target = record(item, `${path}[${index}]`);
    const role = string(target.role, `${path}[${index}].role`);
    const targetStatus = string(target.status, `${path}[${index}].status`);
    if (
      !watchTargetRoles.has(role as WatchTargetRole) ||
      !watchTargetStatuses.has(targetStatus as WatchTargetStatus)
    ) {
      throw new Error(`Collector response has invalid ${path}[${index}]`);
    }
    return {
      path: string(target.path, `${path}[${index}].path`),
      role: role as WatchTargetRole,
      status: targetStatus as WatchTargetStatus,
      message: nullableString(target.message, `${path}[${index}].message`),
    };
  });
}

function normalizeEvent(value: unknown, index: number): FileEvent {
  const path = `events.items[${index}]`;
  const input = record(value, path);
  return {
    id: string(input.id, `${path}.id`),
    observed_at: string(input.observed_at, `${path}.observed_at`),
    path: string(input.path, `${path}.path`),
    destination_path: nullableString(
      input.destination_path,
      `${path}.destination_path`,
    ),
    kind: oneOf<FileEventKind>(
      input.kind,
      ["created", "modified", "deleted", "moved"],
      `${path}.kind`,
    ),
    owner_uid: nullableInteger(input.owner_uid, `${path}.owner_uid`),
    owner_name: nullableString(input.owner_name, `${path}.owner_name`),
    size_before: nullableInteger(input.size_before, `${path}.size_before`),
    size_after: nullableInteger(input.size_after, `${path}.size_after`),
    delta_bytes: nullableNumber(
      input.delta_bytes,
      `${path}.delta_bytes`,
      true,
    ),
    source: string(input.source, `${path}.source`),
  };
}

function normalizeAlert(value: unknown, index: number): StorageAlert {
  const path = `alerts.items[${index}]`;
  const input = record(value, path);
  const rule = oneOf(
    input.rule,
    ["RAPID_FILE_GROWTH", "CAPACITY_PRESSURE"] as const,
    `${path}.rule`,
  );
  const relatedEventIds = stringList(
    input.related_event_ids,
    `${path}.related_event_ids`,
  );
  const common = {
    id: string(input.id, `${path}.id`),
    severity: oneOf(input.severity, ["warning"] as const, `${path}.severity`),
    title: string(input.title, `${path}.title`),
    message: string(input.message, `${path}.message`),
    occurred_at: string(input.occurred_at, `${path}.occurred_at`),
    related_event_ids: relatedEventIds,
  };

  if (rule === "RAPID_FILE_GROWTH") {
    return {
      ...common,
      rule,
      path: string(input.path, `${path}.path`),
      threshold_bytes: integer(input.threshold_bytes, `${path}.threshold_bytes`),
      observed_growth_bytes: integer(
        input.observed_growth_bytes,
        `${path}.observed_growth_bytes`,
      ),
    };
  }

  if (relatedEventIds.length > 0) {
    throw new Error(
      `Collector response has invalid ${path}.related_event_ids for capacity alert`,
    );
  }

  return {
    ...common,
    rule,
    path: string(input.path, `${path}.path`),
    volume_id: string(input.volume_id, `${path}.volume_id`),
    volume_name: string(input.volume_name, `${path}.volume_name`),
    mount_point: string(input.mount_point, `${path}.mount_point`),
    threshold_percent: percentage(
      input.threshold_percent,
      `${path}.threshold_percent`,
      true,
    ),
    observed_percent: percentage(
      input.observed_percent,
      `${path}.observed_percent`,
    ),
    used_bytes: integer(input.used_bytes, `${path}.used_bytes`),
    available_bytes: integer(
      input.available_bytes,
      `${path}.available_bytes`,
    ),
    total_bytes: integer(input.total_bytes, `${path}.total_bytes`),
  };
}

/** Validate API data at the network boundary; never invent missing telemetry. */
export function normalizeDashboardSnapshot(value: unknown): DashboardSnapshot {
  const input = record(value, "dashboard");
  const volumes = record(input.volumes, "volumes");
  const quotas = record(input.quotas, "quotas");
  const io = record(input.io, "io");
  const events = record(input.events, "events");
  const alerts = record(input.alerts, "alerts");
  const aggregate = normalizeRate(io.aggregate, "io.aggregate");

  if (!Array.isArray(volumes.items)) {
    throw new Error("Collector response has invalid volumes.items");
  }
  if (!Array.isArray(io.devices)) {
    throw new Error("Collector response has invalid io.devices");
  }
  if (!Array.isArray(quotas.items)) {
    throw new Error("Collector response has invalid quotas.items");
  }
  if (!Array.isArray(events.items)) {
    throw new Error("Collector response has invalid events.items");
  }
  if (!Array.isArray(alerts.items)) {
    throw new Error("Collector response has invalid alerts.items");
  }

  return {
    sampled_at: string(input.sampled_at, "sampled_at"),
    overall_status: status(input.overall_status, "overall_status"),
    volumes: {
      sampled_at: string(volumes.sampled_at, "volumes.sampled_at"),
      status: status(volumes.status, "volumes.status"),
      source: string(volumes.source, "volumes.source"),
      message: nullableString(volumes.message, "volumes.message"),
      items: volumes.items.map(normalizeVolume),
    },
    quotas: {
      sampled_at: string(quotas.sampled_at, "quotas.sampled_at"),
      status: status(quotas.status, "quotas.status"),
      source: string(quotas.source, "quotas.source"),
      message: nullableString(quotas.message, "quotas.message"),
      user: string(quotas.user, "quotas.user"),
      uid: nullableInteger(quotas.uid, "quotas.uid"),
      items: quotas.items.map(normalizeQuota),
    },
    usage: normalizeUsage(input.usage),
    storage_health: normalizeStorageHealth(input.storage_health),
    io: {
      sampled_at: string(io.sampled_at, "io.sampled_at"),
      status: status(io.status, "io.status"),
      source: string(io.source, "io.source"),
      message: nullableString(io.message, "io.message"),
      interval_seconds: number(io.interval_seconds, "io.interval_seconds"),
      aggregate,
      devices: io.devices.map(normalizeDevice),
    },
    events: {
      sampled_at: string(events.sampled_at, "events.sampled_at"),
      status: status(events.status, "events.status"),
      source: string(events.source, "events.source"),
      message: nullableString(events.message, "events.message"),
      watched_path: string(events.watched_path, "events.watched_path"),
      watch_targets: normalizeWatchTargets(
        events.watch_targets,
        "events.watch_targets",
      ),
      items: events.items.map(normalizeEvent),
    },
    alerts: {
      sampled_at: string(alerts.sampled_at, "alerts.sampled_at"),
      status: status(alerts.status, "alerts.status"),
      source: string(alerts.source, "alerts.source"),
      message: nullableString(alerts.message, "alerts.message"),
      watched_path: string(alerts.watched_path, "alerts.watched_path"),
      watch_targets: normalizeWatchTargets(
        alerts.watch_targets,
        "alerts.watch_targets",
      ),
      threshold_bytes: integer(
        alerts.threshold_bytes,
        "alerts.threshold_bytes",
      ),
      capacity_threshold_percent: percentage(
        alerts.capacity_threshold_percent,
        "alerts.capacity_threshold_percent",
        true,
      ),
      delivery: normalizeDelivery(alerts.delivery),
      items: alerts.items.map(normalizeAlert),
    },
  };
}

export async function getDashboardSnapshot(
  signal?: AbortSignal,
): Promise<DashboardSnapshot> {
  const timeoutController = new AbortController();
  const timeout = window.setTimeout(
    () => timeoutController.abort(),
    REQUEST_TIMEOUT_MS,
  );

  const abortFromParent = () => timeoutController.abort();
  signal?.addEventListener("abort", abortFromParent, { once: true });

  try {
    const response = await fetch(DASHBOARD_ENDPOINT, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: timeoutController.signal,
    });

    if (!response.ok) {
      throw new Error(`Collector returned HTTP ${response.status}`);
    }

    const payload: unknown = await response.json();
    return normalizeDashboardSnapshot(payload);
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", abortFromParent);
  }
}
