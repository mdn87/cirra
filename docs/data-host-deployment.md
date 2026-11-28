# Deploying the two Cirra data hosts

The webhook receiver and worker run as separate processes. Give both the same enrolled private-state path, Cirra release, route configuration and TLS client identity for this data host. Keep their Glas manifests separate. Nodens participates in the quorum through its voter interface. It needs neither a Cirra service configuration nor any of the worker, webhook, witness or routing credentials.

The November 27 production deployment runs on lugos-host and the Intel Mac with Nodens as the third voter. Both receiver/worker pairs, backup schedules and donor refreshes are enabled. The primary audit reader and health checks follow the managed state. [Deployment evidence and operational adjustments](production-rollout-2026-11-27.md)

For upgrades and rollback of that existing deployment, use the
[HA release procedure](ha-deployment.md). The Linux processes are
`cirra-ha-worker.service` and `cirra-ha-webhook.service`, using `ha-config/` and
`ha-launch/`; the original `cirra-worker.service` remains fenced. The remaining
setup examples describe enrollment and templates, not permission to reenroll
the production state or restart the old worker.

## Credentials and observer configuration

1. Bind the same private BWS reference to `CIRRA_ROUTING_KEY` in each data host's **receiver and worker** manifests. Set `coordination.routing_key_env: CIRRA_ROUTING_KEY` in both service configurations. Use an independent random value with at least 32 bytes. Preserve each manifest's existing entries. The receiver also uses the route webhook secrets. The worker uses the configured provider readers, publisher and witness key. Check each host's native Glas profile with its own account. Only reference names and IDs belong in the manifests.
2. Set `coordination.ca`, `certificate` and `private_key` to readable, operator-managed TLS files outside Cirra state, release checkouts and review workspaces. Use the three pinned HTTPS voter endpoints, cluster ID and namespace on both machines. Give each data host a separate client private key readable only by the service account. On Linux the example receiver masks the home directory and the worker exposes selected home paths. A protected directory under `/etc/cirra/` is visible to both.
3. Install the same release and observer image on both machines. Use the same named Docker context in the two observer policies, pointed at each local engine. Run the candidate's `cirra observer-pins --service-config <service.yaml>` on each host and compare both route digests. The definition includes the supervisor source, Docker context, image, argv and limits. A policy change requires draining outstanding work under the old definition.

The homelab candidates supply `CIRRA_ROUTING_KEY` from a dedicated encrypted systemd credential on Linux and an owner-only file on the FileVault-protected Intel Mac. The two hosts exchanged the key directly and verified that it matched with a fresh challenge. Each receiver and worker loads the key after Glas prepares that role's environment. Before starting the Mac services, enroll the separate `cirra-ha` Glas profile with read access to its seven Cirra secrets. Each role checks the permitted Bitwarden secret IDs before loading its receiver, worker or backup-witness bindings. Verify unattended access from the logged-in Mac service session.

## Linux service and recovery paths

The opt-in files under `examples/systemd/ha/` assume user and group `matt` own the worker's private state and `cirra-audit-reader` runs the optional audit reader. The worker uses a managed symlink at `/srv/lugos/state/cirra-ha/current`. Its initial target is the relative path `current.restores/seed`. Each subsequent selected target is a sibling version in `current.restores`. Keep the original single-host tree and all superseded versions. The worker can atomically replace the selector inside the dedicated HA parent. The receiver can write the versioned state and its own Glas home. The backup payload can write only the selected physical version's adjacent capture directory in the state tree.

Use the local storage approved for private state on each data host. The existing Linux primary retains its operator-approved storage configuration. Create `/srv/lugos/state/cirra-ha`, its `current.restores` subdirectory, `/srv/lugos/state/cirra-ha-cache` and `/srv/lugos/state/glas/cirra-standby-refresh` as `matt:matt`, mode `0700`. Install a separately restored and verified initial private tree at `current.restores/seed`; on a new layout create `current` with `ln -s current.restores/seed /srv/lugos/state/cirra-ha/current`. Choose `/srv/lugos/state/cirra-ha-cache/donor.zip` as this host's `coordination.recovery_archive`. Credentials, storage keys and voter TLS files belong outside these directories.

The Linux worker sees a private home. Populate `/etc/cirra-ha/observer-docker` with a separate Docker CLI configuration containing only the named local observer context. For example, create `cirra-ha-observer` with `docker --config <empty-staging-directory> context create cirra-ha-observer --docker host=unix:///var/run/docker.sock`, then install that configuration read-only for the worker at the documented path. The worker override mounts it at `/home/matt/.docker`. Check that `docker --config /etc/cirra-ha/observer-docker --context cirra-ha-observer image inspect <pinned-image>` succeeds as `matt` before restarting.

Set the `--state-dir` arguments in the Linux worker and receiver Glas launcher configurations to the stable selector. For the hourly standby refresh, create the equivalent `cirra-standby-refresh.json` launcher configuration with its own Glas home and a manifest containing just the routing and witness references. Its command is

```text
/srv/lugos/services/cirra/current/.venv/bin/python -I -B -m cirra refresh-standby-snapshot --service-config /srv/lugos/services/cirra/config/service.yaml --state-dir /srv/lugos/state/cirra-ha/current
```

Install the HA unit files and matching `.service.d` directories into `/etc/systemd/system/`. Install `scripts/managed-linux-state.sh` and `scripts/grant-audit-read-access.sh` together as root-owned, mode-0755 files under `/usr/local/libexec/cirra-ha/`. Existing production upgrades use `scripts/deploy-ha.py`, which preserves the managed selector and checks release compatibility. The standalone `deploy-release.sh` does not manage the production HA units. Preserve the runtime queue-format guard through an upgrade or rollback; do not create another enrollment or redirect the selector to the legacy state tree.

For Linux backups, prepare `/etc/cirra-ha/private` as root-owned mode `0700`. Create the following root-owned, mode-0600 configuration files. The systemd manager supplies the credentials to each job separately.

| File | Required content |
| --- | --- |
| `data-host.env` | `CIRRA_LOCAL_DATA_HOST=<64-hex local_data_host>` from this host's `coordination-status` |
| `backup-repository.env` | `RESTIC_REPOSITORY` and the corresponding restic credential, such as `RESTIC_PASSWORD`, for the approved encrypted backup |
| `approved-repository.env` | `CIRRA_APPROVED_BACKUP_REPOSITORY` set to the exact approved off-host location |
| `standby-repository.env` | `RESTIC_REPOSITORY` and `CIRRA_APPROVED_BACKUP_REPOSITORY` for verified donor fetches |
| `standby-restic-password` | The raw restic password for the reader's repository access |

For SFTP, place a minimal OpenSSH configuration, pinned known-hosts file and scoped SSH identity in `private/backup-ssh/` and `private/standby-ssh/` as appropriate. These leaf directories and private keys must be readable by `matt` through the job's read-only bind at `/home/matt/.ssh`. The root-only parent protects the source paths. For HTTPS, make the approved CA and any client certificate available in the corresponding job's namespace. The example template uses the root-peer witness socket. The installed production template instead uses a host-encrypted witness and a private-file adapter to handle long systemd instance names and read-only credential permissions; preserve the [verified deployment adjustments](production-rollout-2026-11-27.md#operational-adjustments). Its timer checks every 15 minutes. The application captures once per six-hour period, independently restores that exact off-host snapshot and records its receipt. The worker and backup both use the selected version's mode-0700, worker-owned `<version>.private-transfer` sibling. Its files stay outside the archived state.

For the optional reader, grant only traversal on `/srv/lugos/state/cirra-ha` and its `current.restores` child with `setfacl -m u:cirra-audit-reader:--x` on each. Enable the `cirra-audit-rebind.path` watcher and `cirra-audit-rebind.service` boot job. It applies per-root audit grants as the state owner, revokes traversal of the previously bound version, and restarts only the running audit reader. The reader checks its bound directory against the live selector on each read and returns HTTP 503 until they match. GitHub and GitLab receivers continue handling requests during an audit rebind. Set the health alert and resolve Glas commands to use the same stable marker at `/srv/lugos/state/glas/cirra-health/health-alert.pending`. The supplied health override reads the selected root. It removes the legacy backup hook that checked receipts in the original single-host tree.

Verify the generated units with `systemd-analyze verify` and reload the manager. Enable `cirra-private-backup.timer` and `cirra-standby-refresh.timer` for the coordinated roots. Verify a new off-host restore receipt from the authoritative host before retiring its old backup schedule. During the first switch or a later binary rollout, keep one **independently reachable, healthy public origin** accepting deliveries throughout each local service restart. Verify the actual webhook route reaches the surviving origin first. [Enrollment and origin checks](coordinated-service.md)

## macOS jobs

Use a host-local release, Python and Glas installation so all three stay available after losing the primary. Prepare private configuration, separate Glas homes, logs and enrolled state before loading these jobs. The renderer takes absolute paths. Its output contains paths and role names. The two BWS manifests contain the actual reference bindings.

```sh
umask 077
base="$HOME/.local/share/cirra-standby"
mkdir -p "$base/logs" "$HOME/Library/LaunchAgents"
for role in webhook worker; do
  python3 "$base/current/scripts/macos-service-plist.py" "$role" \
    --release "$base/current" --python "$base/runtime-3.13/bin/python" \
    --service-config "$base/config/service.yaml" --state "$base/data-current" \
    --manifest "$base/config/cirra-$role.manifest.json" \
    --glas-root "$HOME/.local/share/glas" --glas-home "$base/glas-$role" \
    --logs "$base/logs" > "$base/foo.newman.cirra-$role.plist"
  plutil -lint "$base/foo.newman.cirra-$role.plist"
done
```

After preparing the verified data root, enrolling both services and confirming each account's native credential access, install the two files in `~/Library/LaunchAgents/`. Bootstrap each with `launchctl bootstrap "gui/$(id -u)" <absolute-plist>`. Confirm the receiver through its loopback health endpoint and the worker through its fresh authority-aware heartbeat. A waiting-for-quorum or waiting-for-private-state heartbeat needs investigation. Keep the backup and standby refresh schedules separate from these long-running jobs. macOS FileVault needs a person to unlock the disk after a cold reboot. The current deployment also needs Matt's GUI login for the user agents, Docker engine and reviewer keychain. The Intel Mac completed a fresh subscription-backed reviewer run on November 26, 2026.

## Moving private state back to Linux

1. While the Mac owns the worker lease, continue writing and independently restoring Mac-origin encrypted backups at least every six hours. Authenticate the approved external repository and confirm the verified recovery point matches the Mac's private-state tenure. The original Linux-to-Mac snapshot schedule cannot protect private work completed later on the Mac. Keep all source copies and backup receipts until the restored worker has been verified.
2. To return authority, keep public admission on the surviving receiver, stop the Mac worker cleanly and finish any private writers. Use `freeze-coordination --target-node <preferred-node>` under the source service configuration. This durable checkpoint identifies the exact final private tree and its destination.
3. Capture that frozen tree, restore the verified archive into a **new** private directory on Linux, and check SQLite, current and historical witness signatures, the restored snapshot ID and the frozen private checkpoint. Preserve Linux's previous state directory. Select the new root using the managed state pointer. Only the verified recipient can acquire the next private-state tenure.
4. Recheck worker and receiver health, provider readback for outstanding operations, the first backup from the new authoritative host, and routine standby refresh on the Mac. Restoring an older Linux snapshot cannot undo the handoff. Returning again requires a fresh capture from the current authority.
