import { useEffect, useRef, useState } from "react";
import { getDashboardSnapshot } from "./api";
import type {
  ConnectionState,
  DashboardSnapshot,
  ThroughputSample,
} from "./types";

const POLL_INTERVAL_MS = 1_000;
const MAX_CHART_SAMPLES = 60;

interface DashboardState {
  data: DashboardSnapshot | null;
  connection: ConnectionState;
  error: string | null;
  receivedAt: Date | null;
  samples: ThroughputSample[];
}

export function useDashboard(): DashboardState {
  const [data, setData] = useState<DashboardSnapshot | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [receivedAt, setReceivedAt] = useState<Date | null>(null);
  const [samples, setSamples] = useState<ThroughputSample[]>([]);
  const lastIoSample = useRef<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    const poll = async () => {
      try {
        const snapshot = await getDashboardSnapshot(controller.signal);
        if (controller.signal.aborted) return;

        setData(snapshot);
        setConnection("live");
        setError(null);
        setReceivedAt(new Date());

        const ioIsMeasured =
          snapshot.io.status === "available" || snapshot.io.status === "partial";
        const sampleKey = snapshot.io.sampled_at || snapshot.sampled_at;

        if (ioIsMeasured && sampleKey !== lastIoSample.current) {
          lastIoSample.current = sampleKey;
          setSamples((current) => [
            ...current.slice(-(MAX_CHART_SAMPLES - 1)),
            {
              sampledAt: sampleKey,
              readBytesPerSecond:
                snapshot.io.aggregate.read_bytes_per_second,
              writeBytesPerSecond:
                snapshot.io.aggregate.write_bytes_per_second,
            },
          ]);
        }
      } catch (caught) {
        if (controller.signal.aborted) return;

        setConnection("offline");
        setError(
          caught instanceof Error ? caught.message : "Unable to reach collector",
        );
      } finally {
        if (!controller.signal.aborted) {
          timer = window.setTimeout(poll, POLL_INTERVAL_MS);
        }
      }
    };

    void poll();

    return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  return { data, connection, error, receivedAt, samples };
}
