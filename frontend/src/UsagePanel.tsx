import { clampPercent, formatBytes, statusLabel } from "./format";
import type {
  CollectorStatus,
  ConnectionState,
  OwnerUsage,
  UsageSnapshot,
  UsageTarget,
} from "./types";

interface UsagePanelProps {
  usage: UsageSnapshot | null;
  connection: ConnectionState;
  stale: boolean;
}

function hasData(status: CollectorStatus): boolean {
  return status === "available" || status === "partial";
}

function formatCount(value: number): string {
  return new Intl.NumberFormat().format(value);
}

function formatClock(value: string | null): string {
  if (!value) return "not yet";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "unknown";
  return parsed.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= 3) return path;
  return `…/${parts.slice(-3).join("/")}`;
}

export function UsagePanel({ usage, connection, stale }: UsagePanelProps) {
  const available = usage ? hasData(usage.status) : false;

  return (
    <section
      className={`panel usage-panel${stale ? " is-stale" : ""}`}
      id="usage"
    >
      <div className="panel-heading usage-heading">
        <div>
          <p className="eyebrow">WHOSE FILES HOLD THE MODEL STORAGE</p>
          <h2>Per-owner usage in watched directories</h2>
          <p className="panel-description">
            Bytes grouped by the owning account of each file across every
            watched directory. Ownership at scan time is evidence for
            investigation; it does not prove who performed the writes.
          </p>
        </div>
        <div className="usage-heading-context">
          {usage && (
            <div className="usage-scan">
              <span>Last scan</span>
              <strong>
                {formatClock(usage.scan_started_at)}
                {usage.scan_started_at
                  ? ` · ${usage.scan_duration_seconds.toFixed(1)}s`
                  : ""}
              </strong>
            </div>
          )}
          <span className={`status-chip ${usage?.status ?? "neutral"}`}>
            {usage ? statusLabel[usage.status] : "Connecting"}
          </span>
        </div>
      </div>

      {!usage || !available ? (
        <UsageCapabilityState usage={usage} connection={connection} />
      ) : usage.owners.length === 0 ? (
        <div className="quota-empty" role="status">
          <OwnerIcon />
          <div>
            <strong>No files found in the watched directories</strong>
            <span>
              {usage.message ||
                "The scan completed, but the watched directories contain no regular files."}
            </span>
          </div>
        </div>
      ) : (
        <>
          <div className="usage-totals">
            <div>
              <span>Files scanned</span>
              <strong>{formatCount(usage.file_count)}</strong>
            </div>
            <div>
              <span>Apparent size</span>
              <strong>{formatBytes(usage.total_apparent_bytes)}</strong>
            </div>
            <div>
              <span>Allocated (upper bound)</span>
              <strong>{formatBytes(usage.total_allocated_bytes)}</strong>
            </div>
            <div>
              <span>Owners</span>
              <strong>
                {usage.owner_count > usage.owners.length
                  ? `${usage.owners.length} of ${usage.owner_count}`
                  : usage.owner_count}
              </strong>
            </div>
          </div>

          {usage.status === "partial" && (
            <div className="usage-notice" role="status">
              <strong>Partial scan.</strong> {usage.message}
              {usage.directories.some((target) => target.status !== "scanned") && (
                <ul>
                  {usage.directories
                    .filter((target) => target.status !== "scanned")
                    .map((target) => (
                      <li key={target.path}>
                        <code>{target.path}</code>
                        {" — "}
                        {target.status === "error" ? "not read" : "incomplete"}
                        {target.message ? `: ${target.message}` : ""}
                      </li>
                    ))}
                </ul>
              )}
            </div>
          )}

          <div className="table-scroll">
            <table className="quota-table usage-table">
              <thead>
                <tr>
                  <th scope="col">Owner</th>
                  <th scope="col">Share of scanned bytes</th>
                  <th scope="col">Apparent</th>
                  <th scope="col">Allocated</th>
                  <th scope="col">Files</th>
                  <th scope="col">Largest files</th>
                </tr>
              </thead>
              <tbody>
                {usage.owners.map((owner) => (
                  <OwnerRow key={owner.uid} owner={owner} />
                ))}
              </tbody>
            </table>
          </div>

          <ul className="usage-directories" aria-label="Scanned directories">
            {usage.directories.map((target) => (
              <DirectoryRow key={target.path} target={target} />
            ))}
          </ul>

          <div className="quota-footnote">
            <span>
              <strong>Ownership:</strong> the uid on each file at scan time, not
              the process that wrote it.
            </span>
            <span>
              <strong>Allocated:</strong> summed block counts; an upper bound on
              APFS because clones and sparse files can share blocks.
            </span>
            <span>
              <strong>Scope:</strong> only the watched directories, rescanned
              every {Math.round(usage.scan_interval_seconds)}s. Symlinks are not
              followed and hard links count once.
            </span>
          </div>
        </>
      )}

      <div className="source-line quota-source">
        <span>Source</span>
        <code>{usage?.source || "Waiting for scanner"}</code>
        {stale && <span>Last known usage scan</span>}
      </div>
    </section>
  );
}

function OwnerRow({ owner }: { owner: OwnerUsage }) {
  const share = clampPercent(owner.share_percent);
  return (
    <tr>
      <td>
        <div className="quota-account">
          <span className="quota-account-icon"><OwnerIcon /></span>
          <div>
            <strong>{owner.owner_name ?? "Unknown account"}</strong>
            <span>UID {owner.uid}</span>
          </div>
        </div>
      </td>
      <td>
        <div className="quota-usage">
          <div
            className="mini-progress quota-progress within"
            role="progressbar"
            aria-label={`${owner.owner_name ?? owner.uid} share of scanned bytes`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(share)}
          >
            <span style={{ width: `${share}%` }} />
          </div>
          <span>{owner.share_percent.toFixed(1)}% of scanned bytes</span>
        </div>
      </td>
      <td><strong>{formatBytes(owner.apparent_bytes)}</strong></td>
      <td>{formatBytes(owner.allocated_bytes)}</td>
      <td>{formatCount(owner.file_count)}</td>
      <td>
        <ul className="usage-top-files">
          {owner.top_files.map((file) => (
            <li key={file.path} title={file.path}>
              <code>{shortPath(file.path)}</code>
              <span>{formatBytes(file.apparent_bytes)}</span>
            </li>
          ))}
        </ul>
      </td>
    </tr>
  );
}

function DirectoryRow({ target }: { target: UsageTarget }) {
  return (
    <li className={`usage-directory usage-directory-${target.status}`} title={target.message ?? undefined}>
      <code>{target.path}</code>
      <span>{formatCount(target.file_count)} files · {formatBytes(target.apparent_bytes)}</span>
      <strong>{target.status === "scanned" ? "Scanned" : target.status === "partial" ? "Partial" : "Error"}</strong>
    </li>
  );
}

function UsageCapabilityState({
  usage,
  connection,
}: {
  usage: UsageSnapshot | null;
  connection: ConnectionState;
}) {
  const connecting = connection === "connecting" && !usage;
  const title = connecting
    ? "Connecting to usage scanner"
    : usage?.status === "warming_up"
      ? "First usage scan in progress"
      : usage?.status === "error"
        ? "Usage scan failed"
        : "No watched directory to scan";
  const message =
    usage?.message ||
    (connecting
      ? "Waiting for the first dashboard snapshot."
      : connection === "offline"
        ? "The local collector cannot currently be reached."
        : "The scanner only reports directories the watcher is observing.");

  return (
    <div className={`quota-empty capability ${usage?.status ?? "connecting"}`} role="status">
      <OwnerIcon />
      <div>
        <strong>{title}</strong>
        <span>{message}</span>
        <small>
          LocalTrace does not estimate usage for directories it has not scanned.
        </small>
      </div>
    </div>
  );
}

function OwnerIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20a7 7 0 0 1 14 0" />
    </svg>
  );
}
