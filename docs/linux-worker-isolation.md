# Linux worker isolation

The v1.0.0 release requires a dedicated Linux worker account and rootless Docker.
Removing `SupplementaryGroups=docker` alone is insufficient: the operator's
account already inherits that group and can control other operator processes.

## Provisioning

Use a locked, non-login `cirra-ha` account with its own primary group and no
supplementary groups. On the approved host it has UID 961, GID 955 and the
non-overlapping subordinate UID/GID range `296608:65536`. Check actual host
identities before rendering [the unit override](../examples/systemd/ha/cirra-worker.service.d/rootless.conf).

Its systemd user manager runs the rootless daemon, with linger enabled and
`cpu cpuset io memory pids` delegated. Persistent daemon state lives under
`/srv/lugos/services/cirra/runtime-home`. Docker must report cgroup v2, the
systemd driver and enforceable memory, swap, CPU and process limits. Each
observer execution refuses missing capabilities. Install the independent
[startup guard](../scripts/check-rootless-docker.py) under
`/usr/local/libexec/cirra-ha/` so application rollback cannot remove it.

Initial preparation extracted Debian `rootlesskit` version `2.0.2-2+b9` under
the Cirra service directory, verifying APT metadata and package SHA256
`e9f16eb64017074c2d49f1c6e788aa849070777a5b888fc07e5b57bded051a0c`.
No system package was installed or upgraded. This extracted runtime requires
explicit version/hash updates during maintenance; APT will not update it.
The host already provides `uidmap`, `slirp4netns` and `fuse-overlayfs`.

Transfer the pinned observer image with `docker save`/`docker load` and verify
its exact ID. Keep context name `cirra-ha-observer` on both hosts, selecting
the dedicated Linux socket and existing Mac VM. Observer source changes still
require the managed pin update; admission identity and quotas stay unchanged.

## Privilege boundary

The override clears the old Docker dependency and operator-home mounts. It
removes capabilities, denies namespace creation and hides the rootful socket
and both users' systemd control sockets. `ProtectHome=tmpfs` hides operator
homes and user runtimes; bind only the dedicated runtime directory back in,
with its control sockets masked. A directory bind allows reconnection when
Docker replaces its socket during restart.

The worker has a separate writable home at
`/srv/lugos/services/cirra/worker-home`, a read-only Docker configuration and a
root-owned, hash-pinned Claude executable. No operator hooks, MCP settings,
plugins, history or writable home files are shared. The initial login
credential was copied privately from the authorized subscription. This is a
separate configuration home, not a separate provider account; subscription
expiration or revocation can still interrupt reviews.

A compromised worker retains its own state, assigned credentials and rootless
Docker API. It loses host-root and operator-account authority. Untrusted
checkout commands run in non-root, read-only, network-disabled containers
without credentials or sockets and with bounded resources.

The systemd restrictions are additional protection for normal worker execution.
A compromised coordinator can use its direct rootless API to reach files and
the user manager belonging to the same service UID, bypassing those per-unit
restrictions. The enforced privilege boundary is between that dedicated UID
and root/operator identities. Other service roles use a separate root-owned,
read-only HOME so the worker cannot plant startup files for their processes.

## Account cutover and rollback

Stage and verify the account, engine, image and native login first. Pause the
Mac standby, drain Linux and check the healthy peer webhook origin. Pause
Linux maintenance schedules and wait for active jobs. Hold deployment and
state locks before stopping the local receiver and audit reader.

Copy quiescent state into protected staging, verify every file's bytes and
mode plus SQLite integrity, and change ownership only on the new copy.
Atomically exchange it with the selected physical directory. Keeping the
same path preserves absolute checkout links. Retain the original tree in the
cutover journal. Do not install an older snapshot, reenroll, migrate schemas
or change quorum authority and quota counters.

Move state-owning receiver, backup, refresh and health jobs coherently to the
new account. Set `CIRRA_STATE_USER=cirra-ha` on the root backup selector; its
helper checks ownership before starting the unprivileged capture job. Keep
`cirra-audit-reader` separate with read-only mounts and bearer authentication.
Expose only the credential files each role needs.

Verify native role credentials, quorum access, reader authorization and
receiver health before resuming Linux and then Mac. Verify live worker
health, execution, recovery and monitoring afterward. Rollback after a new
write must transfer the latest selected state to the previous account;
never replace it with the retained pre-cutover copy.

## Staging evidence

On November 28 the proposed systemd restrictions passed a native probe with
UID 961 and only GID 955. Rootful Docker, both user-manager buses, operator
credentials/process environment, daemon configuration and new namespaces
were denied. Capabilities were empty and `NoNewPrivileges` was enabled.
Three real observer tests passed, including in-container cgroup limits and
timeout cleanup. Container UID 65534 mapped to host UID 362141, not root or
the operator. A tool-free Claude response succeeded. These staging results
precede production account cutover and do not alone establish release readiness.
