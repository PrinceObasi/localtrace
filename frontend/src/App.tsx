import { AlertsPanel } from "./AlertsPanel";
import { QuotasPanel } from "./QuotasPanel";
import { UsagePanel } from "./UsagePanel";
import { ThroughputChart } from "./ThroughputChart";
import {
  clampPercent,
  formatBytes,
  formatRate,
  formatTime,
  statusLabel,
  titleCase,
} from "./format";
import type {
  CollectorStatus,
  ConnectionState,
  Volume,
} from "./types";
import { useDashboard } from "./useDashboard";

function App() {
  const { data, connection, error, receivedAt, samples } = useDashboard();
  const primaryVolume = selectPrimaryVolume(data?.volumes.items ?? []);
  const volumeDataAvailable =
    data?.volumes.status === "available" || data?.volumes.status === "partial";
  const alertDataAvailable =
    data?.alerts.status === "available" || data?.alerts.status === "partial";
  const ioDataAvailable =
    data?.io.status === "available" || data?.io.status === "partial";
  const isStale = connection === "offline" && data !== null;
  const capabilities = data
    ? [data.volumes, data.io, data.events, data.alerts, data.quotas, data.usage]
    : [];
  const erroredCapability =
    capabilities.find((capability) => capability.status === "error") ?? null;
  const degradedCapability =
    erroredCapability ??
    capabilities.find((capability) => capability.status !== "available") ??
    null;

  return (
    <div className="app-shell">
      <Sidebar connection={connection} />

      <main className="main-content">
        <header className="topbar">
          <div className="mobile-brand">
            <Logo />
            <span>LocalTrace</span>
          </div>
          <div className="page-title">
            <p>MACOS FILE-SYSTEM OBSERVABILITY</p>
            <h1>Storage overview</h1>
          </div>
          <div className="topbar-status">
            <div className="updated-at">
              <span>Last received</span>
              <strong>{formatTime(receivedAt)}</strong>
            </div>
            <ConnectionPill connection={connection} />
          </div>
        </header>

        <div className="dashboard-content">
          {connection === "offline" && (
            <Notice tone="danger" title="Collector connection lost">
              {error || "LocalTrace could not reach the local collector."}
              {data
                ? " Showing the last successfully received measurements."
                : " Start the API on port 8000 to begin monitoring."}
            </Notice>
          )}

          {data &&
            connection === "live" &&
            (data.overall_status !== "available" || erroredCapability) && (
              <Notice
                tone={erroredCapability ? "danger" : "neutral"}
                title={
                  erroredCapability
                    ? "Collector capability error"
                    : statusLabel[data.overall_status]
                }
              >
                {degradedCapability?.message ||
                  "Some telemetry sources are not currently available."}
              </Notice>
            )}

          <section className="summary-grid" id="overview" aria-label="Summary">
            <CapacityCard
              volume={primaryVolume}
              available={Boolean(volumeDataAvailable && primaryVolume)}
              status={data?.volumes.status ?? null}
              message={data?.volumes.message}
              warningThreshold={
                alertDataAvailable
                  ? data?.alerts.capacity_threshold_percent ?? null
                  : null
              }
              stale={isStale}
            />
            <MetricCard
              eyebrow="PHYSICAL-DEVICE I/O"
              title="Read throughput"
              value={
                ioDataAvailable
                  ? formatRate(data?.io.aggregate.read_bytes_per_second)
                  : "—"
              }
              status={data?.io.status ?? null}
              message={data?.io.message}
              accent="green"
              direction="read"
              stale={isStale}
            />
            <MetricCard
              eyebrow="PHYSICAL-DEVICE I/O"
              title="Write throughput"
              value={
                ioDataAvailable
                  ? formatRate(data?.io.aggregate.write_bytes_per_second)
                  : "—"
              }
              status={data?.io.status ?? null}
              message={data?.io.message}
              accent="blue"
              direction="write"
              stale={isStale}
            />
          </section>

          <section className="panel chart-panel" id="activity">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">LIVE ACTIVITY</p>
                <h2>Physical-device throughput</h2>
                <p className="panel-description">
                  Aggregate device counters; not attributed to a specific APFS
                  volume.
                </p>
              </div>
              <div className="chart-legend" aria-label="Chart legend">
                <span><i className="legend-dot read" />Read</span>
                <span><i className="legend-dot write" />Write</span>
              </div>
            </div>
            <ThroughputChart
              samples={samples}
              status={data?.io.status ?? null}
              message={data?.io.message}
              stale={isStale || !ioDataAvailable}
            />
            <div className="source-line">
              <span>Source</span>
              <code>{data?.io.source || "Waiting for collector"}</code>
              {data?.io.interval_seconds ? (
                <span>{data.io.interval_seconds.toFixed(1)}s measurement window</span>
              ) : null}
            </div>
          </section>

          <AlertsPanel
            alerts={data?.alerts ?? null}
            events={data?.events ?? null}
            connection={connection}
            stale={isStale}
          />

          <UsagePanel
            usage={data?.usage ?? null}
            connection={connection}
            stale={isStale}
          />

          <QuotasPanel
            quotas={data?.quotas ?? null}
            connection={connection}
            stale={isStale}
          />

          <section className="panel volumes-panel" id="volumes">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">INVENTORY</p>
                <h2>Mounted volumes</h2>
                <p className="panel-description">
                  APFS volume allocation is reported separately from shared
                  container capacity.
                </p>
              </div>
              <StatusChip status={data?.volumes.status ?? null} />
            </div>

            {data && data.volumes.items.length > 0 ? (
              <VolumeTable volumes={data.volumes.items} />
            ) : (
              <div className="volume-empty" role="status">
                <DriveIcon />
                <div>
                  <strong>
                    {data?.volumes.status === "warming_up"
                      ? "Discovering mounted volumes"
                      : "No volume inventory available"}
                  </strong>
                  <span>
                    {data?.volumes.message ||
                      (connection === "connecting"
                        ? "Waiting for the local collector."
                        : "The collector returned no mounted volumes.")}
                  </span>
                </div>
              </div>
            )}
          </section>

          <footer className="dashboard-footer">
            <span>LocalTrace runs locally. Telemetry is never sent to the cloud.</span>
            <span>
              Sampled {formatTime(data?.sampled_at ?? null)}
              {isStale ? " · stale" : ""}
            </span>
          </footer>
        </div>
      </main>
    </div>
  );
}

function selectPrimaryVolume(volumes: Volume[]): Volume | null {
  const apfsVolumes = volumes.filter(
    (volume) => volume.filesystem_family === "apfs",
  );
  const exactDataVolume = apfsVolumes.find(
    (volume) => volume.mount_point === "/System/Volumes/Data",
  );
  const dataVolume = apfsVolumes.find(
    (volume) =>
      volume.name.trim().toLowerCase() === "data" ||
      volume.apfs?.roles.some(
        (role) => role.trim().toLowerCase() === "data",
      ),
  );

  return (
    exactDataVolume ??
    dataVolume ??
    volumes.find((volume) => volume.mount_point === "/") ??
    volumes[0] ??
    null
  );
}

function Sidebar({ connection }: { connection: ConnectionState }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <Logo />
        <div>
          <strong>LocalTrace</strong>
          <span>Storage intelligence</span>
        </div>
      </div>
      <nav aria-label="Dashboard sections">
        <a className="nav-link active" href="#overview">
          <OverviewIcon />
          Overview
        </a>
        <a className="nav-link" href="#activity">
          <ActivityIcon />
          Activity
        </a>
        <a className="nav-link" href="#alerts">
          <AlertIcon />
          Alerts
        </a>
        <a className="nav-link" href="#usage">
          <UsageIcon />
          Usage
        </a>
        <a className="nav-link" href="#quotas">
          <QuotaIcon />
          Quotas
        </a>
        <a className="nav-link" href="#volumes">
          <DriveIcon />
          Volumes
        </a>
      </nav>
      <div className="sidebar-spacer" />
      <div className="agent-card">
        <div className="agent-row">
          <span className={`agent-light ${connection}`} />
          <span>LOCAL COLLECTOR</span>
        </div>
        <strong>Loopback API</strong>
        <small>One-second polling</small>
      </div>
    </aside>
  );
}

function CapacityCard({
  volume,
  available,
  status,
  message,
  warningThreshold,
  stale,
}: {
  volume: Volume | null;
  available: boolean;
  status: CollectorStatus | null;
  message?: string | null;
  warningThreshold: number | null;
  stale: boolean;
}) {
  const isApfs = volume?.filesystem_family === "apfs";
  const usedBytes = volume
    ? isApfs
      ? Math.max(0, volume.total_bytes - volume.available_bytes)
      : volume.used_bytes
    : 0;
  const percent = volume
    ? isApfs && volume.total_bytes > 0
      ? clampPercent((usedBytes / volume.total_bytes) * 100)
      : clampPercent(volume.used_percent)
    : 0;
  const warning =
    warningThreshold !== null && percent >= warningThreshold;

  return (
    <article
      className={`summary-card capacity-card${warning ? " has-warning" : ""}${stale ? " is-stale" : ""}`}
    >
      <div className="card-topline">
        <p className="eyebrow">
          {isApfs ? "APFS SHARED CONTAINER" : "PRIMARY MOUNT"}
        </p>
        <DriveIcon />
      </div>
      <h2>{isApfs ? "Container allocation" : "Capacity"}</h2>
      {available && volume ? (
        <>
          <div className="capacity-value">
            <strong>{formatBytes(usedBytes)}</strong>
            <span>
              of {formatBytes(volume.total_bytes)} {isApfs ? "allocated" : "used"}
            </span>
          </div>
          <div
            className="progress-track"
            role="progressbar"
            aria-label={`${volume.name} ${isApfs ? "APFS container allocated" : "capacity used"}`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(percent)}
          >
            <span style={{ width: `${percent}%` }} />
          </div>
          <div className="capacity-meta">
            <span>{percent.toFixed(1)}% {isApfs ? "allocated" : "used"}</span>
            <span>{formatBytes(volume.available_bytes)} available</span>
          </div>
          {warning && (
            <small className="capacity-threshold-warning">
              Above the configured {warningThreshold?.toFixed(0)}% warning threshold
            </small>
          )}
        </>
      ) : (
        <UnavailableValue status={status} message={message} />
      )}
      {stale && <small className="stale-label">Last known measurement</small>}
    </article>
  );
}

function MetricCard({
  eyebrow,
  title,
  value,
  status,
  message,
  accent,
  direction,
  stale,
}: {
  eyebrow: string;
  title: string;
  value: string;
  status: CollectorStatus | null;
  message?: string | null;
  accent: "green" | "blue";
  direction: "read" | "write";
  stale: boolean;
}) {
  const available = status === "available" || status === "partial";

  return (
    <article className={`summary-card metric-card ${accent}${stale ? " is-stale" : ""}`}>
      <div className="card-topline">
        <p className="eyebrow">{eyebrow}</p>
        <ArrowIcon direction={direction} />
      </div>
      <h2>{title}</h2>
      {available ? (
        <div className="metric-value">{value}</div>
      ) : (
        <UnavailableValue status={status} message={message} />
      )}
      <div className="metric-foot">
        <span className={`metric-spark ${accent}`} />
        Aggregate across measured devices
      </div>
      {stale && <small className="stale-label">Last known measurement</small>}
    </article>
  );
}

function UnavailableValue({
  status,
  message,
}: {
  status: CollectorStatus | null;
  message?: string | null;
}) {
  return (
    <div className="unavailable-value">
      <strong>
        {status === "warming_up"
          ? "Warming up"
          : status
            ? statusLabel[status]
            : "Connecting"}
      </strong>
      <span>
        {message ||
          (status === "warming_up"
            ? "Waiting for a valid sample."
            : "No measurement has been reported.")}
      </span>
    </div>
  );
}

function VolumeTable({ volumes }: { volumes: Volume[] }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">Volume</th>
            <th scope="col">File system</th>
            <th scope="col">Capacity</th>
            <th scope="col">Health</th>
            <th scope="col">Access</th>
          </tr>
        </thead>
        <tbody>
          {volumes.map((volume) => (
            <VolumeRows key={volume.id} volume={volume} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function VolumeRows({ volume }: { volume: Volume }) {
  const nfsServer = volume.nfs?.server
    ? volume.nfs.server.includes(":") && !volume.nfs.server.startsWith("[")
      ? `[${volume.nfs.server}]`
      : volume.nfs.server
    : null;
  const nfsLocation =
    nfsServer && volume.nfs?.export
      ? `${nfsServer}:${volume.nfs.export}`
      : nfsServer || volume.nfs?.export || null;
  const isApfs = volume.filesystem_family === "apfs";

  return (
    <>
      <tr>
        <td>
          <div className="volume-name">
            <span className={`volume-icon ${volume.filesystem_family}`}>
              <DriveIcon />
            </span>
            <div>
              <strong>{volume.name || volume.device}</strong>
              <code title={volume.mount_point}>{volume.mount_point}</code>
            </div>
          </div>
        </td>
        <td>
          <div className="stacked-cell">
            <strong>{volume.filesystem.toUpperCase()}</strong>
            <span title={nfsLocation || undefined}>
              {volume.filesystem_family === "nfs"
                ? nfsLocation || volume.device
                : volume.apfs?.container_reference || volume.device}
            </span>
            {volume.filesystem_family === "nfs" && (
              <small>
                {volume.nfs?.protocol_version
                  ? `Protocol ${volume.nfs.protocol_version}`
                  : "Protocol version unavailable"}
              </small>
            )}
          </div>
        </td>
        <td>
          {isApfs ? (
            <div className="table-capacity apfs-capacity">
              <div>
                <strong>{formatBytes(volume.used_bytes)}</strong>
                <small>Volume allocation</small>
              </div>
              <div>
                <span>{formatBytes(volume.available_bytes)}</span>
                <small>Shared available</small>
              </div>
              <div>
                <span>{formatBytes(volume.total_bytes)}</span>
                <small>Container total</small>
              </div>
            </div>
          ) : (
            <div className="table-capacity">
              <div>
                <strong>{formatBytes(volume.used_bytes)}</strong>
                <span> / {formatBytes(volume.total_bytes)}</span>
              </div>
              <div className="mini-progress">
                <span
                  style={{ width: `${clampPercent(volume.used_percent)}%` }}
                />
              </div>
              <small>
                {clampPercent(volume.used_percent).toFixed(1)}% used · {formatBytes(volume.available_bytes)} available
              </small>
            </div>
          )}
        </td>
        <td>
          <HealthBadge volume={volume} />
        </td>
        <td>
          <div className="tag-row">
            {volume.remote && <span className="tag">Remote</span>}
            <span className="tag">
              {volume.read_only ? "Read only" : "Read / write"}
            </span>
            {volume.apfs?.encrypted && <span className="tag">Encrypted</span>}
            {volume.nfs?.protocol_version && (
              <span className="tag">NFS {volume.nfs.protocol_version}</span>
            )}
            {volume.nfs?.status_flags.map((flag) => (
              <span className="tag warning" key={flag}>{titleCase(flag)}</span>
            ))}
          </div>
        </td>
      </tr>
      {volume.filesystem_family === "nfs" && (
        <NfsMetadataRow volume={volume} />
      )}
    </>
  );
}

function NfsMetadataRow({ volume }: { volume: Volume }) {
  const nfs = volume.nfs;

  return (
    <tr className="nfs-details-row">
      <td colSpan={5}>
        <div className="nfs-details">
          <div className="nfs-details-heading">
            <div>
              <span>NFS mount details</span>
              <strong>{volume.mount_point}</strong>
            </div>
            <span className={`status-chip ${nfs?.pnfs_status ?? "unavailable"}`}>
              pNFS {nfs ? statusLabel[nfs.pnfs_status] : "Unavailable"}
            </span>
          </div>

          {nfs ? (
            <>
              <dl className="nfs-facts">
                <div>
                  <dt>Server</dt>
                  <dd title={nfs.server ?? undefined}>{nfs.server ?? "Unavailable"}</dd>
                </div>
                <div>
                  <dt>Export</dt>
                  <dd title={nfs.export ?? undefined}>{nfs.export ?? "Unavailable"}</dd>
                </div>
                <div>
                  <dt>Protocol</dt>
                  <dd>{nfs.protocol_version ?? "Unavailable"}</dd>
                </div>
                <div>
                  <dt>pNFS capability</dt>
                  <dd>{statusLabel[nfs.pnfs_status]}</dd>
                </div>
              </dl>
              <div className="nfs-options">
                <span>Observed mount options</span>
                <div className="tag-row">
                  {nfs.mount_options.length > 0 ? (
                    nfs.mount_options.map((option) => (
                      <code className="tag" key={option}>{option}</code>
                    ))
                  ) : (
                    <em>No mount options reported</em>
                  )}
                </div>
              </div>
              <div className="nfs-kernel-status">
                <span>Kernel warning flags</span>
                <div className="tag-row">
                  {nfs.status_flags.length > 0 ? (
                    nfs.status_flags.map((flag) => (
                      <strong className="tag warning" key={flag}>
                        {titleCase(flag)}
                      </strong>
                    ))
                  ) : (
                    <em>No kernel warning flags observed</em>
                  )}
                </div>
              </div>
              <p className="pnfs-message">{nfs.pnfs_message}</p>
            </>
          ) : (
            <div className="nfs-unavailable" role="status">
              NFS was identified from the mount inventory, but structured server,
              export, protocol and pNFS telemetry were unavailable.
            </div>
          )}
        </div>
      </td>
    </tr>
  );
}

function HealthBadge({ volume }: { volume: Volume }) {
  const health = volume.health;
  const label = health.smart_status
    ? titleCase(health.smart_status)
    : statusLabel[health.status] || titleCase(health.status);
  const healthy =
    health.status === "available" &&
    /^(verified|passed)$/i.test(health.smart_status?.trim() || "");

  return (
    <div className="health-cell" title={health.message || undefined}>
      <span
        className={`health-dot ${
          healthy
            ? "healthy"
            : health.status === "unavailable"
              ? "unavailable"
              : "partial"
        }`}
      />
      <div>
        <strong>{label}</strong>
        {health.message && <span>{health.message}</span>}
      </div>
    </div>
  );
}

function ConnectionPill({ connection }: { connection: ConnectionState }) {
  const label =
    connection === "live"
      ? "Collector live"
      : connection === "offline"
        ? "Collector offline"
        : "Connecting";

  return (
    <div className={`connection-pill ${connection}`} role="status">
      <span />
      {label}
    </div>
  );
}

function StatusChip({ status }: { status: CollectorStatus | null }) {
  if (!status) return <span className="status-chip neutral">Connecting</span>;
  return (
    <span className={`status-chip ${status}`}>
      {statusLabel[status] || titleCase(status)}
    </span>
  );
}

function Notice({
  tone,
  title,
  children,
}: {
  tone: "danger" | "neutral";
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`notice ${tone}`} role="status">
      <span className="notice-icon">!</span>
      <div>
        <strong>{title}</strong>
        <p>{children}</p>
      </div>
    </div>
  );
}

function Logo() {
  return (
    <span className="logo-mark" aria-hidden="true">
      <svg viewBox="0 0 28 28">
        <path d="M5 7.5 14 3l9 4.5v12L14 24l-9-4.5z" />
        <path d="m8 14 3 0 1.7-4 3.1 8 1.6-4H21" />
      </svg>
    </span>
  );
}

function OverviewIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="4" y="4" width="6" height="6" rx="1" />
      <rect x="14" y="4" width="6" height="6" rx="1" />
      <rect x="4" y="14" width="6" height="6" rx="1" />
      <rect x="14" y="14" width="6" height="6" rx="1" />
    </svg>
  );
}

function ActivityIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 12h4l2.2-5 4.1 10 2.2-5H21" />
    </svg>
  );
}

function DriveIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="3" y="5" width="18" height="14" rx="3" />
      <path d="M7 15h.01M11 15h6" />
    </svg>
  );
}

function ArrowIcon({ direction }: { direction: "read" | "write" }) {
  return (
    <svg className="arrow-icon" viewBox="0 0 24 24" aria-hidden="true">
      {direction === "read" ? (
        <path d="M12 4v15m0 0-5-5m5 5 5-5" />
      ) : (
        <path d="M12 20V5m0 0-5 5m5-5 5 5" />
      )}
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 8h18c0-1-3-1-3-8" />
      <path d="M10 20h4" />
    </svg>
  );
}

function UsageIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20a7 7 0 0 1 14 0" />
      <path d="M17 4h4M19 2v4" />
    </svg>
  );
}

function QuotaIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 7h16v11H4z" />
      <path d="M7 11h10M7 15h6" />
      <path d="M8 4h8v3H8z" />
    </svg>
  );
}

export default App;
