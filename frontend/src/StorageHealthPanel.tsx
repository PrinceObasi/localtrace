import { clampPercent, formatBytes, statusLabel } from "./format";
import type {
  ApfsContainer,
  CollectorStatus,
  ConnectionState,
  NvmeDevice,
  ProbeStatus,
  SnapshotGroup,
  StorageHealthSnapshot,
} from "./types";

interface StorageHealthPanelProps {
  health: StorageHealthSnapshot | null;
  connection: ConnectionState;
  stale: boolean;
}

function hasData(status: CollectorStatus): boolean {
  return status === "available" || status === "partial";
}

function formatClock(value: string | null): string {
  if (!value) return "not yet";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "unknown";
  return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "unknown";
  return parsed.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function yesNo(value: boolean | null): string {
  if (value === null) return "—";
  return value ? "Yes" : "No";
}

export function StorageHealthPanel({ health, connection, stale }: StorageHealthPanelProps) {
  const available = health ? health.status !== "warming_up" : false;

  return (
    <section
      className={`panel storage-health-panel${stale ? " is-stale" : ""}`}
      id="health"
    >
      <div className="panel-heading storage-health-heading">
        <div>
          <p className="eyebrow">FILESYSTEM &amp; DEVICE HEALTH</p>
          <h2>Storage health signals</h2>
          <p className="panel-description">
            APFS container state, local Time Machine snapshots holding space,
            and NVMe controller self-reported SMART status. Each probe carries
            its own availability.
          </p>
        </div>
        <div className="usage-heading-context">
          {health && (
            <div className="usage-scan">
              <span>Last refresh</span>
              <strong>{formatClock(health.refreshed_at)}</strong>
            </div>
          )}
          <span className={`status-chip ${health?.status ?? "neutral"}`}>
            {health ? statusLabel[health.status] : "Connecting"}
          </span>
        </div>
      </div>

      {!health || !available ? (
        <div className={`quota-empty capability ${health?.status ?? "connecting"}`} role="status">
          <HeartIcon />
          <div>
            <strong>
              {connection === "connecting" && !health
                ? "Connecting to health probes"
                : "First storage-health refresh in progress"}
            </strong>
            <span>
              {health?.message ||
                (connection === "offline"
                  ? "The local collector cannot currently be reached."
                  : "Waiting for the first dashboard snapshot.")}
            </span>
          </div>
        </div>
      ) : (
        <div className="health-grid">
          <ProbeCard title="APFS containers" probe={health.apfs}>
            {health.apfs.items.map((container) => (
              <ApfsCard key={container.reference} container={container} />
            ))}
          </ProbeCard>
          <ProbeCard title="Local snapshots" probe={health.snapshots}>
            {health.snapshots.items.map((group) => (
              <SnapshotCard key={group.mount_point} group={group} />
            ))}
          </ProbeCard>
          <ProbeCard title="NVMe SMART" probe={health.nvme}>
            {health.nvme.items.map((device) => (
              <NvmeCard key={`${device.bsd_name ?? device.name}`} device={device} />
            ))}
          </ProbeCard>
        </div>
      )}

      <div className="source-line quota-source">
        <span>Source</span>
        <code>{health?.source || "Waiting for probes"}</code>
        {stale && <span>Last known health observation</span>}
      </div>
    </section>
  );
}

function ProbeCard({
  title,
  probe,
  children,
}: {
  title: string;
  probe: ProbeStatus & { items: unknown[] };
  children: React.ReactNode;
}) {
  const showItems = hasData(probe.status) && probe.items.length > 0;
  return (
    <div className={`probe-card probe-${probe.status}`}>
      <div className="probe-heading">
        <strong>{title}</strong>
        <span className={`status-chip ${probe.status}`}>{statusLabel[probe.status]}</span>
      </div>
      {showItems ? <div className="probe-items">{children}</div> : null}
      <p className="probe-message">{probe.message}</p>
    </div>
  );
}

function ApfsCard({ container }: { container: ApfsContainer }) {
  const used = clampPercent(container.used_percent);
  return (
    <div className="probe-item">
      <div className="probe-item-head">
        <code>{container.reference}</code>
        <span>
          {formatBytes(container.capacity_ceiling_bytes - container.capacity_free_bytes)} of{" "}
          {formatBytes(container.capacity_ceiling_bytes)} · {container.used_percent.toFixed(1)}%
        </span>
      </div>
      <div
        className={`mini-progress ${used >= 90 ? "over-limit" : "within"}`}
        role="progressbar"
        aria-label={`${container.reference} container usage`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(used)}
      >
        <span style={{ width: `${used}%` }} />
      </div>
      <div className="probe-kv">
        <span>Physical stores</span>
        <code>{container.physical_stores.join(", ") || "—"}</code>
        <span>Fusion</span>
        <code>{yesNo(container.fusion)}</code>
      </div>
      <table className="probe-table">
        <thead>
          <tr>
            <th>Volume</th>
            <th>Roles</th>
            <th>In use</th>
            <th>APFS quota</th>
            <th>FileVault</th>
            <th>Sealed</th>
          </tr>
        </thead>
        <tbody>
          {container.volumes.map((volume) => (
            <tr key={volume.device} className={volume.sealed?.toLowerCase() === "broken" ? "quota-row-over" : undefined}>
              <td>
                <strong>{volume.name ?? volume.device}</strong>
                <br />
                <code>{volume.device}</code>
              </td>
              <td>{volume.roles.join(", ") || "—"}</td>
              <td>{volume.capacity_in_use_bytes === null ? "—" : formatBytes(volume.capacity_in_use_bytes)}</td>
              <td>
                {volume.capacity_quota_bytes === null ? (
                  <em>none</em>
                ) : (
                  <strong>{formatBytes(volume.capacity_quota_bytes)}</strong>
                )}
                {volume.capacity_reserve_bytes !== null && (
                  <>
                    <br />
                    <small>reserve {formatBytes(volume.capacity_reserve_bytes)}</small>
                  </>
                )}
              </td>
              <td>{yesNo(volume.filevault)}</td>
              <td>{volume.sealed ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SnapshotCard({ group }: { group: SnapshotGroup }) {
  return (
    <div className="probe-item">
      <div className="probe-item-head">
        <code>{group.mount_point}</code>
        <span>
          {group.count} snapshot{group.count === 1 ? "" : "s"}
          {group.volume_group ? ` · ${group.volume_group}` : ""}
        </span>
      </div>
      <div className="probe-kv">
        <span>Oldest</span>
        <code>{formatDate(group.oldest)}</code>
        <span>Newest</span>
        <code>{formatDate(group.newest)}</code>
      </div>
      {group.recent_names.length > 0 && (
        <ul className="probe-list">
          {group.recent_names.map((name) => (
            <li key={name}><code>{name}</code></li>
          ))}
        </ul>
      )}
      <small className="probe-note">
        Snapshot size is not reported by tmutil and is not estimated here.
      </small>
    </div>
  );
}

function NvmeCard({ device }: { device: NvmeDevice }) {
  return (
    <div className="probe-item">
      <div className="probe-item-head">
        <strong>{device.name}</strong>
        <span className={`status-chip ${device.health.status}`}>
          {device.health.smart_status ?? statusLabel[device.health.status]}
        </span>
      </div>
      <div className="probe-kv">
        <span>Device</span>
        <code>{device.bsd_name ?? "—"}</code>
        <span>Size</span>
        <code>{device.size_bytes === null ? "—" : formatBytes(device.size_bytes)}</code>
        <span>TRIM</span>
        <code>{yesNo(device.trim_support)}</code>
        <span>Link</span>
        <code>{[device.link_width, device.link_speed].filter(Boolean).join(" · ") || "—"}</code>
      </div>
      <small className="probe-note">{device.health.message}</small>
    </div>
  );
}

function HeartIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.5A4 4 0 0 1 19 10c0 5.5-7 10-7 10z" />
      <path d="M8 12h2l1.5-3 2 6 1.5-3h2" />
    </svg>
  );
}
