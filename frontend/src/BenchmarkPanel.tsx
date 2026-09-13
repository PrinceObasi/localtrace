import { useState } from "react";

import { startBenchmark } from "./api";
import { formatBytes, statusLabel } from "./format";
import type {
  BenchmarkJob,
  BenchmarksSnapshot,
  ConnectionState,
  Volume,
} from "./types";

interface BenchmarkPanelProps {
  benchmarks: BenchmarksSnapshot | null;
  volumes: Volume[];
  connection: ConnectionState;
  stale: boolean;
}

const SIZE_OPTIONS = [64, 128, 256, 512, 1024].map((mib) => mib * 1024 * 1024);

function formatClock(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "unknown";
  return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function gbps(value: number | undefined): string {
  if (value === undefined) return "—";
  return `${value.toFixed(value >= 10 ? 1 : 2)} GB/s`;
}

export function BenchmarkPanel({ benchmarks, volumes, connection, stale }: BenchmarkPanelProps) {
  const writable = volumes.filter((volume) => !volume.read_only);
  const [mount, setMount] = useState<string>("");
  const [size, setSize] = useState<number>(0);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const selectedMount = mount || writable[0]?.mount_point || "/";
  const maxSize = benchmarks?.max_size_bytes ?? SIZE_OPTIONS[SIZE_OPTIONS.length - 1];
  const sizeChoices = SIZE_OPTIONS.filter((option) => option <= maxSize);
  const selectedSize = size || benchmarks?.default_size_bytes || sizeChoices[0];
  const running = benchmarks?.running ?? false;
  const disabled = submitting || running || connection === "offline" || !benchmarks;

  const run = async () => {
    setError(null);
    setSubmitting(true);
    try {
      await startBenchmark({ mount_point: selectedMount, size_bytes: selectedSize });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className={`panel benchmark-panel${stale ? " is-stale" : ""}`} id="benchmark">
      <div className="panel-heading benchmark-heading">
        <div>
          <p className="eyebrow">FILESYSTEM THROUGHPUT · MEASURED</p>
          <h2>Per-mount read and write in GB/s</h2>
          <p className="panel-description">
            The activity chart above measures physical devices. This probe
            measures a specific APFS or NFS mount by writing a temporary file,
            flushing it to stable storage, and reading it back with the cache
            bypassed. One sequential run, one block size, removed afterwards.
          </p>
        </div>
        <span className={`status-chip ${benchmarks?.status ?? "neutral"}`}>
          {benchmarks ? statusLabel[benchmarks.status] : "Connecting"}
        </span>
      </div>

      <div className="benchmark-form">
        <label>
          <span>Mount</span>
          <select value={selectedMount} onChange={(event) => setMount(event.target.value)} disabled={disabled}>
            {writable.map((volume) => (
              <option key={volume.id} value={volume.mount_point}>
                {volume.mount_point} · {volume.filesystem_family.toUpperCase()}
                {volume.remote ? " · remote" : ""}
              </option>
            ))}
            {writable.length === 0 && <option value="/">/</option>}
          </select>
        </label>
        <label>
          <span>Size</span>
          <select value={selectedSize} onChange={(event) => setSize(Number(event.target.value))} disabled={disabled}>
            {sizeChoices.map((option) => (
              <option key={option} value={option}>{formatBytes(option)}</option>
            ))}
          </select>
        </label>
        <button type="button" className="benchmark-run" onClick={run} disabled={disabled}>
          {running ? "Running…" : submitting ? "Starting…" : "Measure"}
        </button>
        {error && <span className="benchmark-error" role="alert">{error}</span>}
      </div>

      {benchmarks && benchmarks.items.length > 0 ? (
        <div className="table-scroll">
          <table className="quota-table benchmark-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Mount</th>
                <th scope="col">Write</th>
                <th scope="col">Read</th>
                <th scope="col">Size · block</th>
                <th scope="col">Method</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {benchmarks.items.map((job) => (
                <JobRow key={job.id} job={job} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="quota-empty" role="status">
          <GaugeIcon />
          <div>
            <strong>No measurement yet</strong>
            <span>
              {benchmarks?.message ?? "Pick a mount and press Measure to record a throughput sample."}
            </span>
          </div>
        </div>
      )}

      <div className="quota-footnote">
        <span>
          <strong>Write</strong> includes the flush to stable storage
          (<code>F_FULLFSYNC</code> on macOS); flush time is also shown alone.
        </span>
        <span>
          <strong>Read</strong> is taken with <code>F_NOCACHE</code> so it
          measures the filesystem, not the buffer cache. A row says so if that
          was not possible.
        </span>
        <span>
          <strong>Scope:</strong> one sequential run on one mount at one
          moment. Not a benchmark suite; not comparable across block sizes.
        </span>
      </div>

      <div className="source-line quota-source">
        <span>Source</span>
        <code>{benchmarks?.source || "Waiting for collector"}</code>
        {stale && <span>Last known results</span>}
      </div>
    </section>
  );
}

function JobRow({ job }: { job: BenchmarkJob }) {
  const family = job.filesystem_family?.toUpperCase() ?? "?";
  return (
    <tr className={job.status === "failed" ? "quota-row-over" : undefined}>
      <td>{formatClock(job.started_at ?? job.requested_at)}</td>
      <td>
        <strong>{job.mount_point ?? "unknown mount"}</strong>
        <br />
        <small>{family}{job.remote ? " · remote" : ""} · <code title={job.directory}>{job.directory}</code></small>
      </td>
      <td>
        {job.write ? (
          <>
            <strong>{gbps(job.write.gb_per_second)}</strong>
            <br />
            <small>{job.write.seconds.toFixed(2)}s incl. {job.write.flush_seconds?.toFixed(2) ?? "?"}s flush</small>
          </>
        ) : job.status === "running" && job.phase === "write" ? (
          <Progress percent={job.progress_percent} label="writing" />
        ) : (
          "—"
        )}
      </td>
      <td>
        {job.read ? (
          <>
            <strong>{gbps(job.read.gb_per_second)}</strong>
            <br />
            <small>{job.read.seconds.toFixed(2)}s</small>
          </>
        ) : job.status === "running" && job.phase === "read" ? (
          <Progress percent={job.progress_percent} label="reading" />
        ) : (
          "—"
        )}
      </td>
      <td>{formatBytes(job.size_bytes)} · {formatBytes(job.block_bytes)}</td>
      <td>
        <small>
          {job.cache_bypass ? "cache bypassed" : "cache not bypassed"}
          <br />
          flush: {job.flush_method}
        </small>
      </td>
      <td>
        <span className={`status-chip ${job.status === "completed" ? "available" : job.status === "failed" ? "error" : "warming_up"}`}>
          {job.status === "running" ? `${job.phase} ${job.progress_percent.toFixed(0)}%` : job.status}
        </span>
        {job.message && job.status === "failed" && (
          <>
            <br />
            <small>{job.message}</small>
          </>
        )}
      </td>
    </tr>
  );
}

function Progress({ percent, label }: { percent: number; label: string }) {
  return (
    <div className="quota-usage">
      <div className="mini-progress within" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}>
        <span style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
      <span>{label}…</span>
    </div>
  );
}

function GaugeIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 16a8 8 0 0 1 16 0" />
      <path d="M12 16l4-6" />
      <circle cx="12" cy="16" r="1.5" />
    </svg>
  );
}
