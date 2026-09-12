import { formatRate, formatTime } from "./format";
import type { CollectorStatus, ThroughputSample } from "./types";

interface ThroughputChartProps {
  samples: ThroughputSample[];
  status: CollectorStatus | null;
  message?: string | null;
  stale: boolean;
}

interface Point {
  x: number;
  y: number;
}

const WIDTH = 760;
const HEIGHT = 220;
const TOP = 14;
const BOTTOM = 196;
const LEFT = 8;
const RIGHT = 752;

function pointsFor(
  samples: ThroughputSample[],
  key: "readBytesPerSecond" | "writeBytesPerSecond",
  ceiling: number,
): Point[] {
  return samples.map((sample, index) => ({
    x:
      samples.length === 1
        ? RIGHT
        : LEFT + (index / (samples.length - 1)) * (RIGHT - LEFT),
    y: BOTTOM - (sample[key] / ceiling) * (BOTTOM - TOP),
  }));
}

function linePath(points: Point[]): string {
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"}${point.x},${point.y}`)
    .join(" ");
}

function areaPath(points: Point[]): string {
  if (points.length < 2) return "";
  return `${linePath(points)} L${points.at(-1)!.x},${BOTTOM} L${points[0].x},${BOTTOM} Z`;
}

function EmptyChart({
  status,
  message,
}: Pick<ThroughputChartProps, "status" | "message">) {
  const waiting = status === "warming_up";
  const unavailable = status === "unavailable" || status === "error";

  return (
    <div className="chart-empty" role="status">
      <div className={`chart-empty-icon${waiting ? " is-pulsing" : ""}`}>
        {unavailable ? <UnavailableIcon /> : <PulseIcon />}
      </div>
      <strong>
        {waiting
          ? "Establishing an I/O baseline"
          : unavailable
            ? "I/O telemetry unavailable"
            : "Waiting for measured I/O"}
      </strong>
      <span>
        {message ||
          (waiting
            ? "Two physical-device counter samples are required."
            : "The chart will begin when the collector reports a valid sample.")}
      </span>
    </div>
  );
}

export function ThroughputChart({
  samples,
  status,
  message,
  stale,
}: ThroughputChartProps) {
  if (samples.length === 0) {
    return <EmptyChart status={status} message={message} />;
  }

  const rawCeiling = Math.max(
    ...samples.flatMap((sample) => [
      sample.readBytesPerSecond,
      sample.writeBytesPerSecond,
    ]),
  );
  const ceiling = rawCeiling > 0 ? rawCeiling * 1.08 : 1;
  const readPoints = pointsFor(samples, "readBytesPerSecond", ceiling);
  const writePoints = pointsFor(samples, "writeBytesPerSecond", ceiling);
  const latest = samples.at(-1)!;

  return (
    <div className={`chart-wrap${stale ? " is-stale" : ""}`}>
      <div className="chart-scale">
        <span>{formatRate(rawCeiling)}</span>
        <span>0 B/s</span>
      </div>
      <svg
        className="throughput-chart"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Physical-device throughput. Latest read ${formatRate(latest.readBytesPerSecond)}, latest write ${formatRate(latest.writeBytesPerSecond)}.`}
      >
        <defs>
          <linearGradient id="read-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#61dba0" stopOpacity="0.22" />
            <stop offset="1" stopColor="#61dba0" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="write-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#7da8ff" stopOpacity="0.18" />
            <stop offset="1" stopColor="#7da8ff" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[TOP, TOP + 45.5, TOP + 91, TOP + 136.5, BOTTOM].map((y) => (
          <line
            key={y}
            className="chart-grid-line"
            x1={LEFT}
            x2={RIGHT}
            y1={y}
            y2={y}
          />
        ))}
        {readPoints.length > 1 && (
          <path d={areaPath(readPoints)} fill="url(#read-area)" />
        )}
        {writePoints.length > 1 && (
          <path d={areaPath(writePoints)} fill="url(#write-area)" />
        )}
        <path className="chart-line read-line" d={linePath(readPoints)} />
        <path className="chart-line write-line" d={linePath(writePoints)} />
        {readPoints.length === 1 && (
          <circle
            className="read-dot"
            cx={readPoints[0].x}
            cy={readPoints[0].y}
            r="4"
          />
        )}
        {writePoints.length === 1 && (
          <circle
            className="write-dot"
            cx={writePoints[0].x}
            cy={writePoints[0].y}
            r="4"
          />
        )}
      </svg>
      <div className="chart-caption">
        <span>Oldest</span>
        <span>
          {samples.length} measured sample{samples.length === 1 ? "" : "s"}
        </span>
        <span>Now</span>
      </div>
      {stale && (
        <div className="stale-overlay">
          Last measured {formatTime(latest.sampledAt)}
        </div>
      )}
    </div>
  );
}

function PulseIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 12h4l2.2-5 4.1 10 2.2-5H21" />
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
