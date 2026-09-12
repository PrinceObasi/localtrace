# Live NFS validation record

## Verified result

LocalTrace v0.2.1 passed a live loopback NFSv3/TCP mount validation on macOS
26.2. The fixture exported `/private/tmp/localtrace-nfs-export` only to
`127.0.0.1` and mounted it at `/private/tmp/localtrace-nfs-mount`.

![LocalTrace showing the live NFS mount and negotiated metadata](images/localtrace-nfs-live.webp)

The collector reported:

- one live NFS mount with the expected server, export, and mount point;
- negotiated NFS version 3 over TCP;
- `Remote`, `Read / write`, and the current allowlisted mount options;
- no observed kernel warning flags, without translating that into a server
  health verdict;
- backing-device health as unavailable because that must come from the NFS
  server; and
- pNFS as unavailable because the current macOS NFS client does not expose an
  active pNFS data path.

The read/write acceptance independently verified all four operations:

```text
SERVER_READ=PASS
CLIENT_WRITE=PASS
BACKING_EXPORT_VISIBILITY=PASS
CLIENT_DELETE=PASS
NFS_READ_WRITE_ACCEPTANCE=PASS
```

## Evidence boundary

This proves that LocalTrace discovers a live macOS NFS client mount, preserves
its `server:/export` source, enriches it from current negotiated metadata, and
accurately presents its capability limitations. It also proves that the tested
mount supported a bounded read/write/delete cycle.

The read/write acceptance command was separate from LocalTrace. The product
observed and displayed the mounted filesystem; it did not execute the test
writes.

It does **not** prove:

- performance across a physical network;
- health of an independent NFS server or its backing device;
- NFSv4 or NFSv4.1 behavior;
- pNFS support or an active pNFS layout; or
- native NFS quotas, multi-client coherence, locking, durability, server-loss
  recovery, or authentication beyond this `sec=sys` fixture;
- throughput or latency performance; or
- that the displayed capacity belongs to an independent remote disk.

The loopback export is backed by the same APFS storage as the client. Its
reported 93.9% usage therefore mirrored the Mac's backing storage at the time
of capture.

## Safe teardown for the recorded fixture

The preflight found no existing `/etc/exports` file, and `nfsd` was enabled but
not running. The fixture created the two exact temporary directories below and
an `/etc/exports` containing only this line:

```text
/private/tmp/localtrace-nfs-export -mapall=501:20 127.0.0.1
```

Only use the following teardown when those facts still match. It refuses to
remove `/etc/exports` if its contents changed after setup.

```bash
(
  set -eu

  lt_mount=/private/tmp/localtrace-nfs-mount
  lt_export=/private/tmp/localtrace-nfs-export
  lt_marker='LocalTrace NFS loopback validation'
  lt_exports_line='/private/tmp/localtrace-nfs-export -mapall=501:20 127.0.0.1'

  if [ ! -f /etc/exports ] ||
     [ "$(sudo /usr/bin/grep -c '^' /etc/exports)" != '1' ] ||
     ! sudo /usr/bin/grep -qxF "$lt_exports_line" /etc/exports; then
    printf 'STOP: /etc/exports changed; left untouched.\n' >&2
    exit 1
  fi

  if [ "$(/bin/ls -A "$lt_export")" != 'server-marker.txt' ] ||
     ! /usr/bin/grep -qxF "$lt_marker" "$lt_export/server-marker.txt"; then
    printf 'STOP: unexpected export contents; left untouched.\n' >&2
    exit 1
  fi

  if /sbin/mount |
      /usr/bin/grep -F " on $lt_mount (nfs" >/dev/null; then
    sudo /sbin/umount "$lt_mount"
  fi

  if /sbin/mount |
      /usr/bin/grep -F " on $lt_mount (nfs" >/dev/null; then
    printf 'STOP: NFS mount is still active; nothing removed.\n' >&2
    exit 1
  fi

  if [ "$(/bin/ls -A "$lt_export")" != 'server-marker.txt' ] ||
     ! /usr/bin/grep -qxF "$lt_marker" "$lt_export/server-marker.txt"; then
    printf 'STOP: export contents changed; left untouched.\n' >&2
    exit 1
  fi

  sudo /sbin/nfsd stop
  sudo /bin/rm /etc/exports
  /bin/rm "$lt_export/server-marker.txt"
  /bin/rmdir "$lt_mount" "$lt_export"

  printf 'NFS_FIXTURE_TEARDOWN=PASS\n'
)
```

After teardown, `sudo /sbin/nfsd status` should again report that the service
is enabled and not running. Do not disable the service, because it was already
enabled before this validation.

Do not use a forced unmount, `rm -rf`, `nfsd disable`, or delete a pre-existing
or modified exports configuration.
