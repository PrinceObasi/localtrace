import { useMemo, useState } from "react";
import {
  clampPercent,
  formatBytes,
  formatSignedBytes,
  statusLabel,
  titleCase,
} from "./format";
import type {
  AlertDeliveryStatus,
  AlertsSnapshot,
  ConnectionState,
  EventsSnapshot,
  FileEvent,
  StorageAlert,
  WatchTarget,
} from "./types";

interface AlertsPanelProps {
  alerts: AlertsSnapshot | null;
  events: EventsSnapshot | null;
  connection: ConnectionState;
  stale: boolean;
}

function timestamp(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Unknown time";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(parsed);
}

function capabilityHasData(status: string): boolean {
  return status === "available" || status === "partial";
}

export function AlertsPanel({
  alerts,
  events,
  connection,
  stale,
}: AlertsPanelProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const sortedAlerts = useMemo(
    () =>
      [...(alerts?.items ?? [])].sort(
        (left, right) => timestamp(right.occurred_at) - timestamp(left.occurred_at),
      ),
    [alerts?.items],
  );
  const selectedAlert =
    sortedAlerts.find((alert) => alert.id === selectedId) ??
    sortedAlerts[0] ??
    null;
  const relatedEvents = useMemo(() => {
    if (!selectedAlert || selectedAlert.rule !== "RAPID_FILE_GROWTH" || !events) {
      return [];
    }
    const relatedIds = new Set(selectedAlert.related_event_ids);
    return events.items
      .filter((event) => relatedIds.has(event.id))
      .sort(
        (left, right) =>
          timestamp(right.observed_at) - timestamp(left.observed_at),
      );
  }, [events, selectedAlert]);
  const watchedPath = alerts?.watched_path || events?.watched_path || null;
  const watchTargets =
    alerts?.watch_targets?.length
      ? alerts.watch_targets
      : events?.watch_targets ?? [];
  const available = alerts && capabilityHasData(alerts.status);

  return (
    <section className={`panel alerts-panel${stale ? " is-stale" : ""}`} id="alerts">
      <div className="panel-heading alerts-heading">
        <div>
          <p className="eyebrow">DETERMINISTIC ALERTING</p>
          <h2>Alerts &amp; evidence</h2>
          <p className="panel-description">
            Capacity-pressure rules and rapid file growth from observed telemetry.
          </p>
        </div>
        <div className="alerts-context">
          {watchedPath && (
            <WatchSummary watchedPath={watchedPath} targets={watchTargets} />
          )}
          {alerts && capabilityHasData(alerts.status) && (
            <span className="alert-count">
              {alerts.items.length} alert{alerts.items.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
      </div>

      {!available ? (
        <AlertCapabilityState alerts={alerts} connection={connection} />
      ) : sortedAlerts.length === 0 ? (
        <div className="alerts-empty" role="status">
          <span className="empty-shield"><ShieldIcon /></span>
          <strong>No active storage alerts</strong>
          <p>
            LocalTrace will report file growth above {formatBytes(alerts.threshold_bytes)}
            {" "}or mounted-volume usage above {alerts.capacity_threshold_percent.toFixed(1)}%.
          </p>
          {watchedPath && <code>{watchedPath}</code>}
          <WatchTargetList targets={watchTargets} />
          <DeliveryLine delivery={alerts.delivery} />
        </div>
      ) : (
        <div className="investigation-grid">
          <div className="alert-list" aria-label="Storage alerts">
            <div className="alert-list-label">
              <span>Newest first</span>
              {stale && <strong>Last received</strong>}
            </div>
            {sortedAlerts.map((alert) => (
              <AlertRow
                key={alert.id}
                alert={alert}
                selected={selectedAlert?.id === alert.id}
                onSelect={() => setSelectedId(alert.id)}
              />
            ))}
          </div>
          {selectedAlert && (
            <EvidencePanel alert={selectedAlert} events={events} items={relatedEvents} />
          )}
        </div>
      )}
    </section>
  );
}

const roleLabel: Record<WatchTarget["role"], string> = {
  demo: "Demo path",
  model_directory: "Model directory",
  configured: "Configured",
};

function WatchSummary({
  watchedPath,
  targets,
}: {
  watchedPath: string;
  targets: WatchTarget[];
}) {
  const watching = targets.filter((target) => target.status === "watching");
  const count = Math.max(watching.length, 1);
  const tooltip =
    watching.length > 0
      ? watching.map((target) => `${roleLabel[target.role]}: ${target.path}`).join("\n")
      : watchedPath;
  return (
    <div className="watched-path" title={tooltip}>
      <span>
        Watching {count} director{count === 1 ? "y" : "ies"}
      </span>
      <code>{watching.length > 1 ? `${watchedPath} +${watching.length - 1}` : watchedPath}</code>
    </div>
  );
}

function WatchTargetList({ targets }: { targets: WatchTarget[] }) {
  if (targets.length === 0) return null;
  return (
    <ul className="watch-target-list" aria-label="Watched directories">
      {targets.map((target) => (
        <li
          key={`${target.role}:${target.path}`}
          className={`watch-target watch-target-${target.status}`}
          title={target.message ?? undefined}
        >
          <span className="watch-target-role">{roleLabel[target.role]}</span>
          <code>{target.path}</code>
          <span className="watch-target-status">{titleCase(target.status)}</span>
        </li>
      ))}
    </ul>
  );
}

const sinkLabel: Record<string, string> = {
  notification_center: "Notification Center",
  jsonl_log: "JSONL log",
};

function DeliveryLine({ delivery }: { delivery: AlertDeliveryStatus | null }) {
  if (!delivery) return null;
  return (
    <div className={`delivery-line delivery-${delivery.status}`} role="status">
      <span className="delivery-label">Delivery</span>
      <div className="delivery-sinks">
        {delivery.sinks.map((sink) => (
          <span
            key={sink.name}
            className={`delivery-sink delivery-sink-${sink.status}`}
            title={sink.message ?? undefined}
          >
            <strong>{sinkLabel[sink.name] ?? titleCase(sink.name)}</strong>
            {sink.status === "available" ? (
              sink.name === "jsonl_log" && sink.target ? (
                <code>{sink.target}</code>
              ) : (
                <em>on</em>
              )
            ) : (
              <em>{statusLabel[sink.status]}</em>
            )}
          </span>
        ))}
      </div>
      <span className="delivery-count">
        {delivery.delivered_count} delivered
        {delivery.failed_count > 0 ? ` · ${delivery.failed_count} failed` : ""}
      </span>
    </div>
  );
}

function AlertCapabilityState({
  alerts,
  connection,
}: {
  alerts: AlertsSnapshot | null;
  connection: ConnectionState;
}) {
  const connecting = connection === "connecting" && !alerts;
  const title = connecting
    ? "Connecting to alert engine"
    : alerts?.status === "warming_up"
      ? "Alert engine warming up"
      : "Alerts unavailable";
  const message =
    alerts?.message ||
    (connecting
      ? "Waiting for the first dashboard snapshot."
      : connection === "offline"
        ? "The local collector cannot currently be reached."
        : "No alert capability was reported by the collector.");

  return (
    <div className="alerts-empty unavailable" role="status">
      <span className="empty-shield"><UnavailableIcon /></span>
      <strong>{title}</strong>
      <p>{message}</p>
      {alerts && (
        <span className={`status-chip ${alerts.status}`}>
          {statusLabel[alerts.status]}
        </span>
      )}
    </div>
  );
}

function AlertRow({
  alert,
  selected,
  onSelect,
}: {
  alert: StorageAlert;
  selected: boolean;
  onSelect: () => void;
}) {
  const isCapacity = alert.rule === "CAPACITY_PRESSURE";

  return (
    <button
      className={`alert-row ${isCapacity ? "capacity-alert" : "growth-alert"}${selected ? " selected" : ""}`}
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
    >
      <div className="alert-row-top">
        <span className={`severity-pill ${alert.severity}`}>
          {alert.severity}
        </span>
        <time dateTime={alert.occurred_at}>{formatDateTime(alert.occurred_at)}</time>
      </div>
      <strong>{alert.title}</strong>
      <code className="alert-rule">{alert.rule}</code>
      <span className="alert-path" title={alert.path}>
        {isCapacity
          ? `${alert.volume_name} · ${alert.mount_point}`
          : alert.path}
      </span>
      <div className="alert-growth">
        <span>{isCapacity ? "Observed capacity" : "Observed growth"}</span>
        <b>
          {isCapacity
            ? `${alert.observed_percent.toFixed(1)}% used`
            : formatBytes(alert.observed_growth_bytes)}
        </b>
      </div>
    </button>
  );
}

function EvidencePanel({
  alert,
  events,
  items,
}: {
  alert: StorageAlert;
  events: EventsSnapshot | null;
  items: FileEvent[];
}) {
  if (alert.rule === "CAPACITY_PRESSURE") {
    return <CapacityEvidence alert={alert} />;
  }

  const eventsAvailable = events && capabilityHasData(events.status);

  return (
    <article className="evidence-panel">
      <div className="evidence-heading">
        <div>
          <p className="eyebrow">SELECTED ALERT</p>
          <h3>What changed?</h3>
        </div>
        <span className="evidence-count">
          {alert.related_event_ids.length} related ID
          {alert.related_event_ids.length === 1 ? "" : "s"}
        </span>
      </div>

      <div className="alert-explanation">
        <strong>{alert.message}</strong>
        <div>
          <span>
            Rule <code>{alert.rule}</code>
          </span>
          <span>
            Threshold <b>{formatBytes(alert.threshold_bytes)}</b>
          </span>
          <span>
            Observed <b>{formatBytes(alert.observed_growth_bytes)}</b>
          </span>
        </div>
      </div>

      {!eventsAvailable ? (
        <div className="evidence-state" role="status">
          <UnavailableIcon />
          <div>
            <strong>File-event evidence unavailable</strong>
            <span>
              {events?.message ||
                "The watcher did not provide file-event evidence for this snapshot."}
            </span>
          </div>
        </div>
      ) : items.length === 0 ? (
        <div className="evidence-state" role="status">
          <LinkIcon />
          <div>
            <strong>No related events currently retained</strong>
            <span>
              This alert contains no IDs matching the real events in the current
              dashboard snapshot.
            </span>
          </div>
        </div>
      ) : (
        <div className="evidence-list">
          {items.map((event) => (
            <FileEvidence key={event.id} event={event} />
          ))}
        </div>
      )}

      <div className="ownership-note">
        File ownership is evidence about the path—not proof of which user or process
        performed the write.
      </div>
    </article>
  );
}

function CapacityEvidence({
  alert,
}: {
  alert: Extract<StorageAlert, { rule: "CAPACITY_PRESSURE" }>;
}) {
  const percent = clampPercent(alert.observed_percent);

  return (
    <article className="evidence-panel capacity-evidence">
      <div className="evidence-heading">
        <div>
          <p className="eyebrow">SELECTED ALERT</p>
          <h3>Capacity context</h3>
        </div>
        <span className="evidence-count capacity">Volume telemetry</span>
      </div>

      <div className="alert-explanation capacity">
        <strong>{alert.message}</strong>
        <div>
          <span>
            Rule <code>{alert.rule}</code>
          </span>
          <span>
            Mount <code>{alert.mount_point}</code>
          </span>
        </div>
      </div>

      <div className="capacity-evidence-card">
        <div className="capacity-evidence-title">
          <div>
            <span>Mounted volume</span>
            <strong>{alert.volume_name}</strong>
          </div>
          <code title={alert.mount_point}>{alert.mount_point}</code>
        </div>
        <div
          className="capacity-alert-track"
          role="progressbar"
          aria-label={`${alert.volume_name} capacity used`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(percent)}
        >
          <span style={{ width: `${percent}%` }} />
          <i style={{ left: `${clampPercent(alert.threshold_percent)}%` }} />
        </div>
        <div className="capacity-track-labels">
          <span>{alert.observed_percent.toFixed(1)}% observed</span>
          <span>{alert.threshold_percent.toFixed(1)}% warning threshold</span>
        </div>
        <dl className="capacity-evidence-facts">
          <div>
            <dt>Used</dt>
            <dd>{formatBytes(alert.used_bytes)}</dd>
          </div>
          <div>
            <dt>Available</dt>
            <dd>{formatBytes(alert.available_bytes)}</dd>
          </div>
          <div>
            <dt>Total</dt>
            <dd>{formatBytes(alert.total_bytes)}</dd>
          </div>
          <div>
            <dt>Volume ID</dt>
            <dd title={alert.volume_id}>{alert.volume_id}</dd>
          </div>
        </dl>
      </div>

      <div className="capacity-observation-note">
        This is an observational threshold warning. It does not block writes or
        claim that the filesystem has failed. File-change evidence does not apply
        to this volume-level alert.
      </div>
    </article>
  );
}

function FileEvidence({ event }: { event: FileEvent }) {
  const owner = event.owner_name
    ? `${event.owner_name}${event.owner_uid === null ? "" : ` · UID ${event.owner_uid}`}`
    : event.owner_uid === null
      ? "Unavailable"
      : `UID ${event.owner_uid}`;

  return (
    <div className="file-evidence">
      <div className="file-evidence-top">
        <span className={`event-kind ${event.kind}`}>{titleCase(event.kind)}</span>
        <time dateTime={event.observed_at}>{formatDateTime(event.observed_at)}</time>
      </div>
      <code className="evidence-path" title={event.path}>{event.path}</code>
      {event.destination_path && (
        <div className="destination-path">
          <span>Moved to</span>
          <code title={event.destination_path}>{event.destination_path}</code>
        </div>
      )}
      <dl className="evidence-facts">
        <div>
          <dt>Size delta</dt>
          <dd className={event.delta_bytes !== null && event.delta_bytes > 0 ? "positive" : ""}>
            {formatSignedBytes(event.delta_bytes)}
          </dd>
        </div>
        <div>
          <dt>Resulting size</dt>
          <dd>{event.size_after === null ? "Unavailable" : formatBytes(event.size_after)}</dd>
        </div>
        <div>
          <dt>File owner</dt>
          <dd>{owner}</dd>
        </div>
      </dl>
      <div className="event-source">
        <span>Observed by</span>
        <code>{event.source}</code>
      </div>
    </div>
  );
}

function ShieldIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 3 5 6v5c0 4.5 2.8 8 7 10 4.2-2 7-5.5 7-10V6z" />
      <path d="m9 12 2 2 4-5" />
    </svg>
  );
}

function UnavailableIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="8" />
      <path d="m7 7 10 10" />
    </svg>
  );
}

function LinkIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m10 13.5 4-3" />
      <path d="M8.5 16.5 6 19a3 3 0 0 1-4-4l4-4a3 3 0 0 1 4 0" />
      <path d="m15.5 7.5 2.5-2.5a3 3 0 0 1 4 4l-4 4a3 3 0 0 1-4 0" />
    </svg>
  );
}
