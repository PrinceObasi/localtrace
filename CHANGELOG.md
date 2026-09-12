# Changelog

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
