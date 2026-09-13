# LocalTrace Architecture

## Current phase

LocalTrace is in its hackathon MVP phase: prove trustworthy, end-to-end macOS
storage-monitoring paths before broadening protocol coverage. The v0.2
demonstration makes a local storage workload visible as capacity and I/O
telemetry, shows native current-user quota state and observed NFS metadata, and
raises explainable rapid-growth and capacity-pressure alerts.

The first slice consists of:

| Component | Responsibility |
| --- | --- |
| Python collector/API (`backend/`) | Read host capabilities and telemetry, normalize results, and expose a loopback-only HTTP API |
| React/Vite dashboard (`frontend/`) | Poll the API and present administrator-oriented status, metrics, and limitations |
| Demo workload (`scripts/`) | Produce a bounded, marked `.gguf`-like file using observable chunked writes |
| Evidence service (`backend/`) | Watch the demo directory plus existing local-AI model directories and any configured extras, retain bounded file events, and raise a deterministic rapid-growth alert |
| Quota collector (`backend/`) | Parse `/usr/bin/quota -uv` for the process's current account, expose rows with nonzero reported quota fields, and label their filesystem-dependent semantics |
| Usage scanner (`backend/`) | Rescan watched directories in a bounded background thread and roll up bytes, file counts, and largest files by owning uid |
| Filesystem benchmark (`backend/`) | Run one bounded write/flush/read probe at a time on a chosen mount and keep the last twenty results with phase progress |
| Storage health collector (`backend/`) | Refresh APFS container detail, local snapshot counts, and NVMe SMART in a background thread; each probe reports its own capability state |
| Alert delivery (`backend/`) | Queue each newly raised alert and deliver it off-thread to Notification Center and an append-only JSONL log, reporting sink state without ever dropping the alert |
| Capacity alert service (`backend/`) | Evaluate each real volume snapshot, retain a single alert per threshold crossing, and re-arm only after an observed recovery |

The dashboard requests `GET /api/v1/dashboard`. During development, Vite
proxies `/api` to the backend on `127.0.0.1:8000`. No cloud service is required.

## Metric semantics

LocalTrace must label what a metric actually proves:

- **Capacity** is reported by a mounted filesystem. APFS volumes can share an
  APFS container, so a volume's available space is not an isolated physical
  allocation.
- **I/O throughput** is a rate derived from differences between cumulative
  device counters over a measured interval. A physical-device counter is not
  automatically attributable to one APFS volume, process, user, or file.
- **File changes** are evidence that a path changed near an event. Ownership of
  the resulting file does not prove which user or process performed the write,
  and temporal correlation does not prove causation.
- **Watch targets** carry their own state. The demo path is created if
  missing because the workload writes there. Every other target (a well-known
  model directory or a `LOCALTRACE_WATCH_PATHS` entry) is read-only and is
  watched only if it already exists as a real directory. A missing default
  model directory is `skipped` and does not degrade the capability; a missing
  or failed *configured* path makes the capability `partial`, because the
  administrator asked for it explicitly. Targets are scheduled parents-first
  regardless of the order they were configured in, so a nested or duplicate
  target is skipped as covered and one change is never recorded twice; a
  configured parent of the demo path is watched and the demo path is marked
  covered by it.
- **Quota** must identify its source. A native filesystem or NFS quota is
  distinct from a LocalTrace policy threshold; the latter is an alerting budget
  and does not enforce writes. v0.2 queries only the process's current account.
  A row without any nonzero limit is not emitted as a configured quota, and
  LocalTrace does not use that empty observation to infer whether the
  filesystem supports or has disabled quota policy. Each emitted row carries
  `limit_semantics`: APFS values are `absolute_limit`; macOS NFSv4 values are
  `remaining_availability`; and unverified filesystem meanings are `unknown`.
  Because macOS may label an NFSv4 mount only as `nfs`, the aggregated
  dashboard reconciles the quota row's exact normalized mount path with the
  volume collector's current negotiated NFS version. Only absolute limits may
  be used to derive a used-versus-limit percentage.
  Starred block/file overage evidence and printed grace values remain explicit
  fields rather than being reconstructed from ambiguous numbers.
- **Per-owner usage** is a scan of the watched directories only. Apparent
  bytes sum `st_size`; allocated bytes sum `st_blocks * 512` and are an upper
  bound on APFS because clones and sparse files can share or skip blocks.
  Symbolic links are never followed, so a Hugging Face snapshot link and its
  blob count once, and hard links are deduplicated by `(st_dev, st_ino)`
  within a scan. The scanner stops at a file budget or a time budget and
  reports the result as `partial` and `truncated` rather than presenting a
  visited subset as the whole directory. The clock is checked on every
  directory entry, but a `stat` blocked inside the kernel (a stale hard NFS
  mount) cannot be interrupted, so the time budget is a bound on cooperative
  work, not a hard deadline. More owners than the table shows also makes the
  result `partial`, with `owner_count` carrying the true number. The first
  snapshot is `warming_up` and neither that nor `unavailable` (no watched
  directory) degrades the dashboard; only a failed scan does.
- **Alert delivery** is a separate capability from alert evaluation. Both
  rules call one listener when they append a new alert; the listener only
  enqueues, so evaluation never waits on `osascript` or disk. Delivery starts
  before the watcher, and an alert raised before the worker is running waits
  in the queue rather than being delivered on the raising thread. The worker
  delivers each alert id once and records the outcome per sink: an available
  sink whose last post failed is reported as `error` with the reason, never
  as "on". The alert-level counters distinguish fully delivered, partially
  delivered (some sinks failed), failed, and still queued. The alert itself
  stays in the in-memory store and the dashboard regardless. A
  non-macOS host or `LOCALTRACE_NOTIFY=0` makes the Notification Center sink
  `unavailable`, which is not a failure.
- **Filesystem throughput** is active measurement, kept separate from the
  passive device counters. A run is attributed to the deepest mount point
  containing its directory, records whether `F_NOCACHE` and `F_FULLFSYNC`
  were engaged, and includes flush time in the write figure. It never runs
  unrequested, never runs concurrently with another, and never leaves its
  file behind.
- **Storage-health probes** are independent. `diskutil apfs list` supplies
  container ceiling/free space and per-volume native APFS quotas, reserves,
  FileVault, lock, and seal state; a `Broken` seal marks the probe `partial`.
  `tmutil listlocalsnapshots` supplies counts and timestamps only, because
  it does not report size. `system_profiler SPNVMeDataType` supplies
  NVMe-only controller metadata and a device-reported SMART value mapped by
  the same function the per-mount `diskutil` path uses. Serial numbers and
  partition children are dropped before anything is retained. A single
  failed probe makes the aggregate `partial`; only three failures make it
  `error`, and only `error` degrades the dashboard.
- **Health** is capability-dependent. SMART/NVMe information may not be exposed
  for every Apple or external device. Missing data must never be translated to
  `Healthy`.
- **NFS/pNFS** support must distinguish observed live mount metadata, parsed
  test fixtures, and planned support. LocalTrace extracts the server, export,
  protocol version, and allowlisted current mount options from
  `/usr/bin/nfsstat -v -f JSON -m <mountpoint>`, falling back to the mount
  record when enrichment is unavailable. It requests the complete mount table
  and filters known pseudo-filesystem types itself, because psutil's macOS
  `all=False` behavior drops NFS sources such as `server:/export`. Raw JSON,
  filehandles, principals, and realms are discarded. Only the `dead`, `not
  responding`, and `recovery`
  status flags are exposed; an empty list means no kernel warning flag was
  observed, not that the remote server is healthy. The current macOS NFS client
  is built without pNFS support, so v0.2 reports pNFS as `unavailable` on
  macOS. An NFSv4/NFSv4.1 label, multiple filesystem locations, a layout-named
  counter, or a pNFS-looking option does not demonstrate an active pNFS data
  path.
- **Stale remote mounts** remain an operating-system boundary. Capacity reads
  call `statvfs` through `psutil.disk_usage()` and a stale hard NFS mount can
  block inside that kernel call. A Python thread timeout would only abandon a
  worker while leaving it blocked, so v0.2 documents this limitation rather
  than presenting the call as cancellable.
- **Capacity pressure** is evaluated from a mounted filesystem's used
  percentage. APFS volumes with the same container reference are evaluated as
  one shared-capacity key. For APFS, container use is derived as shared total
  minus shared available space, and its percentage is derived against that
  total; the raw per-volume allocation fields remain unchanged. The alert uses
  a stable representative that prefers the writable Data mount, role, or name.
  Other mounts use their stable volume key. The rule emits once
  at/above the warning threshold, remains deduplicated while the key stays
  active, and re-arms only after a real observation reaches the lower recovery
  boundary. The default 90% warning and 88% recovery boundaries provide two
  percentage points of hysteresis. An unavailable, failed, or missing volume
  sample is not treated as recovery.

Sampling interval, source, units, and timestamp should travel with each metric
so the UI can explain it without overstating precision.

## Capability and unavailable states

Each optional probe should fail independently and return an explicit state:

| State | Meaning |
| --- | --- |
| `available` | The host exposed the value and collection succeeded |
| `partial` | Some useful evidence is available, but it does not establish the complete claim |
| `warming_up` | A rate or state requires another real sample before it is meaningful |
| `unavailable` | The host, current device, account, or mount did not expose the capability |
| `error` | Collection was attempted but failed; include a safe diagnostic |

The API should still return useful partial results when an optional probe is
unavailable. The dashboard should show these states directly instead of using
zero, an empty chart, or a green health indicator as a substitute for unknown
data.

## v0.2 request flow

`GET /api/v1/dashboard` gathers the volume, I/O, and quota collectors without
turning one optional-probe failure into a total response failure. The usage
scanner is not run per request; the dashboard serves its newest completed
background result so a large scan cannot block one-second polling. The resulting
volume snapshot is then evaluated by the capacity alert service. File evidence
and rapid-growth alerts come from the bounded in-memory evidence service. The
combined response carries the status, source, message, units, and timestamp
needed for the dashboard to label each claim honestly.

The individual quota contract is also available at `GET /api/v1/quotas`.
Quota subprocess calls use the absolute `/usr/bin/quota -uv` invocation,
argument arrays, a timeout, an accepted-output size cap after capture, and a
short cache so one-second dashboard polling does not run the command every
second. The trusted native utility's pipe is still captured before that cap is
applied; v0.2 does not claim a streaming memory bound. A successful command
that reports `none` or only rows without nonzero limits is `available` with an
empty item list. `unavailable` is reserved for a platform or command that
cannot provide the probe; an empty successful result is not proof that the
filesystem lacks quota support. NFSv4 remaining-availability fields are exposed
as reported without deriving utilization from them. The dashboard reconciles
them against the volume snapshot collected in the same request. The individual
quota route uses a previously sampled volume snapshot when one exists and does
not trigger an additional potentially blocking mount-capacity read; before a
volume sample, a generic `nfs` row therefore remains `unknown`. A normal
unsupported or empty quota observation remains visible in the quota capability
without degrading the entire dashboard; an actual quota collector `error`
contributes to the aggregate health state.

The watched/demo leaf-directory checks and exclusive file creation prevent
common accidental overwrite and symlink cases. They are not a race-free
sandbox against a hostile process running as the same account; stronger
hardening would use directory descriptors and no-follow operations throughout.

## Validation status and near-term sequence

1. **Completed:** real APFS capacity, physical-device I/O, FSEvents evidence,
   current-user quota probing, and deterministic alerts were validated on the
   demonstration Mac.
2. **Completed:** a live loopback NFSv3/TCP mount on macOS 26.2 was discovered,
   enriched from current `nfsstat` data, displayed in the dashboard, and passed
   server-read, client-write, backing-export-visibility, and delete checks.
   The filesystem acceptance check was separate from the observational
   LocalTrace collector.
   Because the export used the same Mac, its capacity mirrors the backing APFS
   storage and is not an independent remote-disk measurement.
3. **Next:** validate against an independent remote NFS server when one is
   available, then add sustained-I/O and capacity-runway rules alongside the
   implemented rapid-growth and capacity-pressure rules.
4. Preserve recorded fixtures for environments where CI cannot expose a live
   NFS mount, and detect any future macOS pNFS support before making a
   negotiated-data-path claim.
