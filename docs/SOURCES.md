# Platform evidence used by LocalTrace

LocalTrace keeps platform claims tied to Apple's released source instead of
inferring capabilities from names in a mount record.

## NFS and pNFS

- Apple's current released [`nfs(5)` source][apple-nfs-man] says directly that
  pNFS is not supported by the macOS NFSv4.1 client.
- The released client's [`EXCHANGE_ID` implementation][apple-exchange-id]
  sends `NFS_EXCHGID4_FLAG_USE_NON_PNFS`.
- Apple's client tests mark the old [`fpnfs` option][apple-fpnfs-test] as
  deprecated and expect it to fail as unavailable.
- Apple's [`nfsstat(1)` source][apple-nfsstat-man] documents JSON mount output;
  its implementation distinguishes [current negotiated parameters and
  status][apple-nfsstat-current] from originally requested values.

Therefore LocalTrace v0.2 reports pNFS as `unavailable` on macOS. It can show
NFSv4.1, multiple filesystem locations, layout-named counters, or raw mount
options, but none of those observations overrides the platform limitation or
proves that a pNFS layout was negotiated.

## Native quotas

- With no account argument, Apple's [`quota` implementation][apple-quota-main]
  queries the current user by default.
- The [`-v` output implementation][apple-quota-output] prints 1 KiB block
  usage, block soft/hard limits, file usage, and file soft/hard limits. A
  nonzero limit is required before a limit check is active; over-limit values
  are marked in the output.
- The collector implementation enumerates mounted filesystems, calls
  [`quotactl(..., Q_GETQUOTA, ...)`][apple-quota-probe], and skips mounts that do
  not provide usable quota data.
- Apple's NFSv4 client maps the protocol's
  [`quota_avail_soft`/`quota_avail_hard` attributes][apple-nfs4-quota-map] into
  the local soft/hard fields. RFC 8881 defines those attributes as
  [additional available bytes][rfc8881-quota], not absolute quota ceilings.

Therefore LocalTrace queries only its process's current account, converts the
reported 1 KiB block values to bytes, shows only rows with at least one nonzero
limit field, and distinguishes a successful empty result from an
unavailable probe. It preserves the printed over-limit and grace evidence.
APFS rows are labeled as absolute limits; NFSv4 fields are labeled as remaining
availability and are not used to calculate utilization; unverified filesystem
semantics stay unknown. An empty result means only that no reportable nonzero
current-user record was observed; it does not establish whether the filesystem
lacks or has disabled quota support. LocalTrace does not create, change, or
enforce quota policy.

[apple-nfs-man]: https://github.com/apple-oss-distributions/NFS/blob/NFS-343.100.5/files/nfs.5#L93-L97
[apple-exchange-id]: https://github.com/apple-oss-distributions/NFS/blob/NFS-343.100.5/kext/nfs4_vnops.c#L9921-L10028
[apple-fpnfs-test]: https://github.com/apple-oss-distributions/NFS/blob/NFS-343.100.5/nfsclntTests/nfsclntTests.m#L366-L372
[apple-nfsstat-man]: https://github.com/apple-oss-distributions/NFS/blob/NFS-343.100.5/nfsstat/nfsstat.1#L60-L97
[apple-nfsstat-current]: https://github.com/apple-oss-distributions/NFS/blob/NFS-343.100.5/nfsstat/nfsstat.c#L1593-L1656
[apple-quota-main]: https://github.com/apple-oss-distributions/diskdev_cmds/blob/diskdev_cmds-757/quota.tproj/quota.c#L116-L187
[apple-quota-output]: https://github.com/apple-oss-distributions/diskdev_cmds/blob/diskdev_cmds-757/quota.tproj/quota.c#L313-L456
[apple-quota-probe]: https://github.com/apple-oss-distributions/diskdev_cmds/blob/diskdev_cmds-757/quota.tproj/quota.c#L570-L633
[apple-nfs4-quota-map]: https://github.com/apple-oss-distributions/NFS/blob/NFS-343.100.5/kext/nfs4_subs.c#L2492-L2504
[rfc8881-quota]: https://www.rfc-editor.org/rfc/rfc8881.html#section-5.8.2.28
