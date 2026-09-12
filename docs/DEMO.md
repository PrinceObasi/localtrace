# LocalTrace demonstration guide

## Ninety-second judge flow

### 0:00–0:15 — The problem

> Local AI workloads can download and generate tens of gigabytes without an
> administrator immediately knowing which workload changed storage behavior.
> LocalTrace is a local-first flight recorder for that problem on macOS.

### 0:15–0:38 — Establish that the data is real

Open the dashboard and point out:

- the actual macOS volume name and filesystem;
- current used and available capacity;
- physical-device read and write throughput;
- the current account's native quota state; and
- for an NFS mount, the observed server, export, protocol, mount options, and
  evidence-labeled pNFS status.

Say **physical-device throughput**, not per-volume throughput.

If no native limits exist, show the successful empty quota result and explain
that it means no reportable nonzero current-user record was observed—not that
the filesystem lacks quota support. If no NFS mount exists, call out that no
live NFS metadata is present rather than presenting a fixture as live data.
For an NFSv4 quota row in the dashboard, point out the **remaining
availability** label and explain that it comes from matching the quota's exact
mount path to the currently negotiated NFS version. Do not describe its
numeric fields as absolute caps or a utilization percentage.
If an NFS mount has no warning flags, say that no kernel warning flag was
observed; do not describe the remote server as healthy.

### 0:38–1:06 — Trigger the event

Run:

```bash
make demo
```

Show the throughput chart reacting to the write. Open the resulting rapid-growth
alert and show the changed `.gguf` path, its size delta, and its file owner.

### 1:06–1:22 — Explain the second alert rule

Point to the configured capacity threshold. Explain that LocalTrace raises one
alert when a volume crosses it, suppresses duplicates while the volume remains
high, and re-arms only at the lower recovery boundary (88% for the default 90%
warning). APFS volumes sharing a container are evaluated together using shared
total and available space, so their per-volume allocations do not create
duplicate or misleading incidents. If you lowered either boundary
for the demo, say so plainly. This is an observational alert, not a filesystem
quota and not write enforcement.

### 1:22–1:30 — Close

> LocalTrace does more than graph storage. It connects a storage incident to
> the files and users an administrator should investigate next, while staying
> completely local to the Mac.

## Pre-demo checklist

- [ ] `make setup` succeeds from a fresh checkout.
- [ ] `make test` passes.
- [ ] `make run` starts both processes.
- [ ] The dashboard shows the actual startup volume.
- [ ] The I/O chart changes during `make demo`.
- [ ] One rapid-growth alert appears during `make demo`.
- [ ] **What changed?** contains evidence related to that alert.
- [ ] Current-user quota state is visible; a successful probe with no
      reportable nonzero record is `Available` with an empty list, not a
      fabricated zero-byte quota.
- [ ] Any NFS row shows its real mount metadata and labels pNFS unavailable on
      the current macOS client, even if an option contains a pNFS-looking name.
- [ ] Any mounted NFS export is present in the volume list; its `server:/export`
      source was not lost during mount enumeration.
- [ ] Capacity alerts remain deduplicated across repeated dashboard polls and
      re-arm only at the configured lower recovery boundary.
- [ ] Unsupported metrics display `Unavailable`, not zero or healthy.
- [ ] Browser zoom and terminal font are readable from several feet away.
- [ ] A backup screen recording is available.
- [ ] The repository is public before submission.

## Claims to avoid

- Do not call physical-device counters per-volume I/O.
- Do not call file ownership proof of writer identity.
- Do not call LocalTrace policy thresholds native filesystem quotas.
- Do not claim an active pNFS path on the current macOS NFS client.
- Do not infer pNFS from NFSv4/NFSv4.1, multiple filesystem locations,
  layout-named counters, or pNFS-looking options.
- Do not call an empty NFS warning-flag list proof that the server is healthy.
- Do not describe current-user quota visibility as multi-user policy
  enforcement.
- Do not claim an empty successful quota result proves quota support is absent
  or disabled.
- Do not calculate or claim an NFSv4 quota utilization percentage from fields
  that macOS reports with remaining-availability semantics.
- Do not claim an unavailable health signal is healthy.
