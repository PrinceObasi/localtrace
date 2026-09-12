# Changelog

## 0.3.0 — 2026-09-12

### Added

- The evidence service now watches existing local-AI model directories
  (Ollama, Hugging Face hub, LM Studio, llama.cpp, Exo) in addition to the
  demo path, so real workload downloads and conversions feed the same
  rapid-growth rule.
- `LOCALTRACE_WATCH_PATHS` (colon-separated) adds administrator-chosen
  directories; `LOCALTRACE_WATCH_MODEL_DIRS=0` disables the defaults. Both are
  exposed as `make run WATCH_PATHS=... WATCH_MODEL_DIRS=...`.
- `watch_targets` on the events and alerts responses reports each directory's
  role and whether it is `watching`, `skipped`, or `error`, with the reason.
- The dashboard alerts panel shows how many directories are watched and lists
  every target with its state.
- A background usage scanner rolls up bytes, file counts, share of the scanned
  total, and largest files by owning uid across every watched directory,
  exposed at `GET /api/v1/usage`, in the dashboard response, and in a new
  **Per-owner usage** panel. Scan cadence and file/time budgets are
  configurable through `make run USAGE_SCAN_INTERVAL_SECONDS=... USAGE_MAX_FILES=...
  USAGE_MAX_SECONDS=...`.
- The macOS CI smoke check now scans a real temporary directory and verifies
  owner attribution and symlink handling.

### Accuracy and safety

- Additional directories are read-only observations and are never created; a
  missing well-known model directory is a normal `skipped` state, not an
  error, while a missing explicitly configured path is `partial`.
- Nested and duplicate targets are skipped as already covered, and
  symbolic-link directories are refused for every target.
- Usage scans never follow symbolic links, count hard links once, label
  allocated bytes as an APFS upper bound, and report budget-limited scans as
  `partial` and `truncated` instead of presenting a visited subset as complete.

## 0.2.1 — 2026-09-12

### Accuracy hotfix

- APFS capacity pressure is calculated once per shared container from the
  container's reported total and available bytes, instead of borrowing one
  sibling volume's allocation percentage.
- The overview prioritizes the writable macOS Data volume as the representative
  for its APFS container and presents shared allocation separately from
  per-volume allocation.
- APFS capacity cards and alerts now use the same denominator and warning
  threshold, preventing a green System-volume card beside a Data-volume alert.
- Binary byte formatting is labeled with IEC units (`KiB`, `MiB`, `GiB`) rather
  than decimal unit names.

## 0.2.0 — 2026-09-12

### Added

- NFS server, export, protocol-version, and mount-option metadata.
- Allowlisted NFS `dead`, `not responding`, and `recovery` warning flags, with
  an explicit warning that an empty list is not a server-health verdict.
- Evidence-labeled pNFS capability reporting that identifies pNFS as
  unsupported by the current macOS NFS client and does not infer negotiation
  from a protocol version or mount option.
- Native current-user block and file quota visibility through macOS
  `/usr/bin/quota -uv`. Successful no-record/all-zero output is represented as
  an available empty result; unsupported probe capability remains unavailable.
- Filesystem-aware quota semantics, including absolute APFS limits, macOS
  NFSv4 remaining-availability values, preserved over-limit/grace evidence, and
  an explicit unknown state where the meaning is not verified.
- A stateful capacity-pressure rule that emits once per threshold crossing and
  re-arms only after an observed lower-boundary recovery, with two percentage
  points of hysteresis by default and APFS shared-container deduplication.
- `GET /api/v1/quotas` and quota data in the aggregated dashboard response.
- Dashboard panels for native quotas, richer NFS context, and capacity alerts.
- A macOS CI smoke path for the quota collector that does not require a
  quota-configured runner.

### Accuracy and safety

- Native quota visibility remains read-only and current-account scoped;
  LocalTrace does not enforce filesystem policy.
- Complete mount-table enumeration plus an explicit pseudo-filesystem filter
  preserves NFS `server:/export` sources that psutil's macOS default omits.
- Generic macOS `nfs` quota rows are labeled only after an exact mount-path
  join to a currently observed negotiated NFS version; ambiguous versions stay
  unknown.
- Optional telemetry remains explicitly `partial`, `unavailable`, or `error`
  instead of being replaced by fabricated zero or healthy values.
- Capacity alerts retain their active state across transient unavailable/error
  samples, preventing a failed probe from being mistaken for recovery.
- Loopback Host-header validation reduces DNS-rebinding exposure, while the
  watched/demo leaf directory rejects symbolic links and demo files use
  exclusive mode-`0600` creation.

## 0.1.0 — 2026-09-12

- Initial mounted-volume, APFS metadata, physical-device I/O, watched-path
  evidence, rapid-growth alert, and local React dashboard vertical slice.
