import { clampPercent, formatBytes, statusLabel } from "./format";
import type {
  CollectorStatus,
  ConnectionState,
  QuotaEntry,
  QuotasSnapshot,
} from "./types";

interface QuotasPanelProps {
  quotas: QuotasSnapshot | null;
  connection: ConnectionState;
  stale: boolean;
}

function hasData(status: CollectorStatus): boolean {
  return status === "available" || status === "partial";
}

function formatCount(value: number): string {
  return new Intl.NumberFormat().format(value);
}

function scopeLabel(entry: QuotaEntry): string {
  if (entry.filesystem_family === "nfs") return "Native NFS user quota";
  if (entry.filesystem_family === "apfs") return "Native quota on APFS";
  return "Native current-user quota";
}

function semanticsLabel(entry: QuotaEntry): string {
  if (entry.limit_semantics === "absolute_limit") return "Absolute limits";
  if (entry.limit_semantics === "remaining_availability") {
    return "Remaining availability fields";
  }
  return "Limit interpretation withheld";
}

function percentOfLimit(used: number, entry: QuotaEntry): number | null {
  if (entry.limit_semantics !== "absolute_limit") return null;
  const limit = entry.hard_limit_bytes ?? entry.soft_limit_bytes;
  if (limit === null || limit <= 0) return null;
  return (used / limit) * 100;
}

export function QuotasPanel({
  quotas,
  connection,
  stale,
}: QuotasPanelProps) {
  const available = quotas ? hasData(quotas.status) : false;

  return (
    <section
      className={`panel quotas-panel${stale ? " is-stale" : ""}`}
      id="quotas"
    >
      <div className="panel-heading quotas-heading">
        <div>
          <p className="eyebrow">NATIVE QUOTA REPORT</p>
          <h2>Filesystem quota records</h2>
          <p className="panel-description">
            Quota fields reported by mounted filesystems for the current account.
            Semantics are labeled per row; LocalTrace does not create or enforce
            these values.
          </p>
        </div>
        <div className="quota-heading-context">
          {quotas && (
            <div className="quota-subject">
              <span>Account queried</span>
              <strong>
                {quotas.user || "Unknown account"}
                {quotas.uid === null ? "" : ` · UID ${quotas.uid}`}
              </strong>
            </div>
          )}
          <QuotaStatus status={quotas?.status ?? null} />
        </div>
      </div>

      {!quotas || !available ? (
        <QuotaCapabilityState quotas={quotas} connection={connection} />
      ) : quotas.items.length === 0 ? (
        <div className="quota-empty" role="status">
          <QuotaIcon />
          <div>
            <strong>No reportable nonzero native quota record</strong>
            <span>
              {quotas.message ||
                "The quota probe completed, but no nonzero block or file limits were reported."}
            </span>
            <small>
              This is different from unavailable telemetry and does not mean a
              LocalTrace policy threshold is active.
            </small>
          </div>
        </div>
      ) : (
        <>
          <div className="table-scroll">
            <table className="quota-table">
              <thead>
                <tr>
                  <th scope="col">Account &amp; scope</th>
                  <th scope="col">Filesystem</th>
                  <th scope="col">Block usage</th>
                  <th scope="col">Block limits</th>
                  <th scope="col">File usage</th>
                  <th scope="col">File limits</th>
                </tr>
              </thead>
              <tbody>
                {quotas.items.map((entry, index) => (
                  <QuotaRow
                    key={`${entry.filesystem}:${entry.mount_point ?? "unknown"}:${entry.uid ?? entry.user}:${index}`}
                    entry={entry}
                  />
                ))}
              </tbody>
            </table>
          </div>
          <div className="quota-footnote">
            <span>
              <strong>Native report:</strong> values come from the filesystem
              quota interface named in the source.
            </span>
            <span>
              <strong>Not policy:</strong> capacity alert thresholds are
              observational and do not block writes.
            </span>
            <span>
              <strong>NFS semantics:</strong> remaining-availability fields are
              shown as reported; no used-to-limit ratio is inferred.
            </span>
          </div>
        </>
      )}

      <div className="source-line quota-source">
        <span>Source</span>
        <code>{quotas?.source || "Waiting for collector"}</code>
        {stale && <span>Last known quota observation</span>}
      </div>
    </section>
  );
}

function QuotaRow({ entry }: { entry: QuotaEntry }) {
  const limitPercent = percentOfLimit(entry.used_bytes, entry);
  const rowOverLimit = entry.block_over_limit || entry.file_over_limit;
  const ratioUnavailable =
    entry.limit_semantics === "remaining_availability"
      ? "NFS reports remaining availability; ratio withheld"
      : entry.limit_semantics === "unknown"
        ? "Limit semantics unknown; ratio withheld"
        : "No configured block limit reported";

  return (
    <tr className={rowOverLimit ? "quota-row-over" : undefined}>
      <td>
        <div className="quota-account">
          <span className="quota-account-icon"><UserIcon /></span>
          <div>
            <strong>{entry.user || "Unknown account"}</strong>
            <span>{entry.uid === null ? "UID unavailable" : `UID ${entry.uid}`}</span>
            <small>{scopeLabel(entry)}</small>
          </div>
        </div>
      </td>
      <td>
        <div className="stacked-cell quota-filesystem">
          <strong>{entry.filesystem_family.toUpperCase()}</strong>
          <code title={entry.filesystem}>{entry.filesystem}</code>
          {entry.mount_point && entry.mount_point !== entry.filesystem && (
            <code title={entry.mount_point}>Mounted at {entry.mount_point}</code>
          )}
          <small>{semanticsLabel(entry)}</small>
        </div>
      </td>
      <td>
        <div className="quota-usage">
          <strong>{formatBytes(entry.used_bytes)}</strong>
          {limitPercent === null ? (
            <span>{ratioUnavailable}</span>
          ) : (
            <>
              <div
                className={`mini-progress quota-progress ${entry.block_over_limit ? "over-limit" : "within"}`}
                role="progressbar"
                aria-label={`${entry.user} block quota usage`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(clampPercent(limitPercent))}
              >
                <span style={{ width: `${clampPercent(limitPercent)}%` }} />
              </div>
              <span>{limitPercent.toFixed(1)}% of reported limit</span>
            </>
          )}
          {entry.block_over_limit && (
            <strong className="quota-overage">Native output marks block quota exceeded</strong>
          )}
        </div>
      </td>
      <td>
        <LimitPair
          soft={entry.soft_limit_bytes}
          hard={entry.hard_limit_bytes}
          formatter={formatBytes}
          semantics={entry.limit_semantics}
          grace={entry.block_grace}
        />
      </td>
      <td>
        <div className="quota-usage">
          <strong>{formatCount(entry.files_used)} files</strong>
          <span>Reported inode/file usage</span>
          {entry.file_over_limit && (
            <strong className="quota-overage">Native output marks file quota exceeded</strong>
          )}
        </div>
      </td>
      <td>
        <LimitPair
          soft={entry.file_soft_limit}
          hard={entry.file_hard_limit}
          formatter={formatCount}
          semantics={entry.limit_semantics}
          grace={entry.file_grace}
        />
      </td>
    </tr>
  );
}

function LimitPair({
  soft,
  hard,
  formatter,
  semantics,
  grace,
}: {
  soft: number | null;
  hard: number | null;
  formatter: (value: number) => string;
  semantics: QuotaEntry["limit_semantics"];
  grace: string | null;
}) {
  const softLabel =
    semantics === "remaining_availability"
      ? "Soft avail."
      : semantics === "unknown"
        ? "Soft field"
        : "Soft";
  const hardLabel =
    semantics === "remaining_availability"
      ? "Hard avail."
      : semantics === "unknown"
        ? "Hard field"
        : "Hard";

  return (
    <dl className="quota-limits">
      <div>
        <dt>{softLabel}</dt>
        <dd>{soft === null || soft === 0 ? "Not configured" : formatter(soft)}</dd>
      </div>
      <div>
        <dt>{hardLabel}</dt>
        <dd>{hard === null || hard === 0 ? "Not configured" : formatter(hard)}</dd>
      </div>
      {grace && (
        <div>
          <dt>Grace</dt>
          <dd>{grace}</dd>
        </div>
      )}
    </dl>
  );
}

function QuotaCapabilityState({
  quotas,
  connection,
}: {
  quotas: QuotasSnapshot | null;
  connection: ConnectionState;
}) {
  const connecting = connection === "connecting" && !quotas;
  const title = connecting
    ? "Connecting to quota probe"
    : quotas?.status === "warming_up"
      ? "Quota probe warming up"
      : quotas?.status === "error"
        ? "Quota probe failed"
        : "Native quota telemetry unavailable";
  const message =
    quotas?.message ||
    (connecting
      ? "Waiting for the first dashboard snapshot."
      : connection === "offline"
        ? "The local collector cannot currently be reached."
        : "This filesystem or account did not expose native quota information.");

  return (
    <div className={`quota-empty capability ${quotas?.status ?? "connecting"}`} role="status">
      <QuotaIcon />
      <div>
        <strong>{title}</strong>
        <span>{message}</span>
        <small>
          LocalTrace will not replace unavailable values with estimated usage or
          policy limits.
        </small>
      </div>
    </div>
  );
}

function QuotaStatus({ status }: { status: CollectorStatus | null }) {
  return (
    <span className={`status-chip ${status ?? "neutral"}`}>
      {status ? statusLabel[status] : "Connecting"}
    </span>
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

function UserIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="8" r="3" />
      <path d="M5 20c.6-4 2.9-6 7-6s6.4 2 7 6" />
    </svg>
  );
}
