# Recovering Cirra state

The recovery job takes a consistent off-host copy of the service database, durable queues, audit history, observer evidence and publication receipts. It then restores the entire snapshot to new private scratch and checks the recovered data. The restic repository must already exist on a separate host and have credentials available independently of the Cirra host.

Matt allows up to 24 hours of data loss and requires Cirra to keep serving through loss of its primary host. The six-hour online backup leaves time to recover from a missed capture. The scheduled November 26 06:30 UTC capture recorded zero seconds of service pause. Restoration took 9.70 seconds, passed SQLite integrity and foreign-key checks, and verified 194 current and 273 historical witness signatures. The Intel Mac recovered the same database independently. Public admission and review publication still depend on the primary host.

## Install the recovery job

The scheduled backup uses GLAS to fetch the same witness key that signs the worker's receipts. Give the backup its own manifest containing that single BWS reference. On lugos-host, run the following from the Cirra checkout. The temporary file contains a reference ID.

```sh
runtime=/srv/lugos/runtimes/glas/config
manifest=$(mktemp)
sudo install -d -o root -g root -m 0755 /etc/cirra
jq -e '
  .consumer.id = "cirra-backup-witness" |
  .consumer.name = "Cirra backup verification" |
  .secrets |= map(select(.env == "CIRRA_WITNESS_KEY")) |
  select((.secrets | length) == 1)
' "$runtime/cirra-worker.manifest.json" > "$manifest" &&
  sudo install -o root -g root -m 0644 "$manifest" /etc/cirra/cirra-backup.manifest.json
sudo install -o root -g root -m 0644 examples/glas/cirra-backup.json /etc/cirra/
```

The recovery unit runs as root. It opens a root-owned, mode-0600 local Unix socket. Systemd starts the BWS lookup under the service account for that connection. The witness helper checks the client's kernel-reported UID and sends the key over the socket. The root process holds it in memory for verification. Restic and systemctl receive their usual environment. The independent stop hook can still resume services from an interrupted legacy backup.

Install the helpers and unit files before starting the first backup. Online capture uses Linux inotify, rsync, SQLite's backup API and restic. Keep the installed recovery and health helpers in a root-owned directory.

```sh
sudo install -o root -g root -m 0755 scripts/backup-state.py /usr/local/libexec/cirra-backup-state.py
sudo install -o root -g root -m 0755 scripts/backup-health.py /usr/local/libexec/cirra-backup-health.py
sudo install -o root -g root -m 0755 scripts/backup-witness.py /usr/local/libexec/cirra-backup-witness.py
sudo install -o root -g root -m 0644 examples/systemd/backup/cirra-backup-witness{.socket,@.service} /etc/systemd/system/
sudo install -o root -g root -m 0644 examples/systemd/cirra-state-backup.{service,timer} /etc/systemd/system/
sudo install -D -o root -g root -m 0644 examples/systemd/cirra-state-backup.service.d/bws-witness.conf \
  /etc/systemd/system/cirra-state-backup.service.d/bws-witness.conf
```

The example reads restic's existing protected environment file and a separate root-owned, mode-0600 `/etc/cirra-state-backup.conf`. Set `CIRRA_APPROVED_BACKUP_REPOSITORY` there to the exact restic repository location approved for Cirra private state. The helper checks that it matches `RESTIC_REPOSITORY` before invoking restic. Keep restic credentials, the witness key and publisher or webhook credentials outside the state tree. Secure the offline configuration archive and its recovery passphrase under separate access controls.

The default state is `/srv/lugos/state/cirra`. The script includes this one directory, stays on its filesystem, and omits the operator scratch directories `acceptance` and `operator-recovery`. The service's configuration, secrets and other applications' state are outside the snapshot. For a replacement host without BWS access, pass a separately escrowed credential file to `verify` as shown below. Existing installations using a protected witness file can continue to pass `--witness-file`.

The service needs systemd, restic with `--retry-lock` and restore verification, access to the existing SFTP or storage credentials, and a Linux kernel that supports `tmpfs,noswap`. Verification restores to the service's private, non-executable tmpfs. Set its size for the uncompressed state and available RAM. The mount disappears when the service exits. The live state and backup snapshots remain intact. Multiple runs share a lock, and the snapshot also takes the deployment lock while work is paused.

Start and verify the first backup. Then enable monitoring on this host by installing the drop-in for the existing five-minute Cirra health check. Install the same drop-in when upgrading a host with an enabled backup timer.

```sh
sudo systemctl daemon-reload
sudo systemctl start cirra-state-backup.service
sudo cat /var/lib/cirra-recovery/last-verified.json
sudo install -D -o root -g root -m 0644 examples/systemd/cirra-health.service.d/state-backup.conf /etc/systemd/system/cirra-health.service.d/state-backup.conf
sudo systemctl daemon-reload
sudo systemctl enable --now cirra-state-backup.timer
```

The timer runs at 00:30, 06:30, 12:30 and 18:30 UTC and catches a missed run after boot. A failed run retries after 15 minutes, up to the configured start limit. An encrypted restic snapshot on a separate LAN host protects against loss of the service disk. A site disaster requires an independent off-site copy.

The five-minute health check alerts if verification evidence is missing or unreadable, the backup service reports failure, or the last verified capture exceeds the 24-hour data-loss limit. A failed run records a private timestamp under `/var/lib/cirra-recovery`. Starting a retry or verifying an older snapshot leaves the alert active. A newly captured and successfully restored backup clears the backup problem. The normal alert resolves once the worker and backup checks both pass.

Only the root-owned health helper reads the recovery receipt. The health journal and the operator event receive a short reason without the snapshot ID, repository location or credentials. Inspect `journalctl -u cirra-state-backup.service` locally for the failed run. A host without scheduled backups uses the ordinary health service without the drop-in.

## Snapshot and verification

1. The scheduled `online` action watches the local Linux state tree while rsync copies durable files to private scratch. SQLite supplies a consistent copy of committed transactions. A changed durable file, a lost watch or an overflow invalidates the capture. The helper checks files with hard links separately and retries a concurrent change up to twice. The recovered queues must have no running job. Malformed queue records remain available for diagnosis.
2. The helper excludes acceptance scratch, operator recovery files, transient heartbeat data, unpublished temporary receipts and raw SQLite journals. It verifies the private copy and uploads it with its original logical path and a source-specific tag. Production services continue running throughout. The explicit legacy `backup` action still quiesces the original active units and uses the recovery journal to resume them.
3. The helper verifies the full snapshot ID, tag and source path, then calls `restic restore --verify` into a new directory. It runs SQLite's integrity and foreign-key checks using a read-only connection, verifies the expected Cirra tables, and checks all current and historical observer signatures using the separate witness credential. Historical receipts must also match their content-addressed filenames. Restored code and provider actions are never executed.
4. Root-only receipts under `/var/lib/cirra-recovery/` contain the backup identity, quiescence time, verification time, schema version and witness counts. `pause_seconds` covers the backup and return of the service units. `restore_verify_seconds` measures restoration and validation from Nodens or the configured repository. `restore_point_age_seconds` measures the age of the captured state at the end of that drill. The interval since the last verified capture determines exposure at a later failure.

## Reviewing off-host retention

The verified receipt records total and free bytes on the restore filesystem while the restored copy is still present. The five-minute health check raises a capacity alert when that copy leaves less than 5% free, capped at a 1 GiB reserve. On lugos-host the service uses a 1 GiB private tmpfs. If it approaches the reserve, check host memory headroom and increase the `size=` setting in the backup unit's `TemporaryFileSystem` mount. Reload systemd and confirm the next verified receipt shows the new capacity. Preserve the `noswap` mount option when resizing.

The scheduled job keeps its tagged Cirra snapshots on Nodens. Monthly maintenance includes checking the oldest and newest recovery points with `restic snapshots --tag cirra-state` and checking free space on Nodens. Record the latest successful restore alongside those figures. The host archive's 14-copy rotation applies to separate local archives. Expiration of Cirra's off-host snapshots requires a retention decision for Nodens.

## Restoring service after host loss

Matt approved `intelmac` as the first standby for `lugos-host`. It has local FileVault storage, an amd64 Linux Docker engine and a running Cloudflare connector. Private standby state belongs on its local disk. Nodens holds the encrypted restic repository and also provides LAN DNS. The 4070 PC has no standby assignment.

The Intel Mac checks the primary's verified recovery point every hour while the operator's macOS login session is active. Its sole transfer key is restricted to the Mac's inventoried source address and a fixed export command. The primary reads its own root-owned verification receipt and streams only that snapshot's Cirra directory from the approved restic repository. The client supplies its last snapshot ID so a successful check can skip an unchanged transfer. The transfer credentials cannot select an arbitrary backup, path or shell command.

The local file `~/.local/share/cirra-standby/current.zip` contains `state.tar` and `verified.json`. A complete transfer, valid archive checksums, a successful source restore and a capture within 24 hours are required to replace it. A failed transfer preserves the previous file. The transfer keeps 2 GiB of disk space in reserve. The encrypted history remains on Nodens. To check freshness while the primary is unreachable, run this on the Mac.

```sh
root="$HOME/.local/share/cirra-standby"
/usr/local/bin/python3.13 -I "$root/standby-snapshot.py" status
launchctl print "gui/$(id -u)/foo.newman.cirra-standby-refresh"
```

For recovery, inspect `verified.json` and unpack the tar into new private scratch. The tar includes the logical path `srv/lugos/state/cirra`. On November 26 the Mac checked the scheduled copy with outbound networking disabled. SQLite reported 44 audits, 130 saved reports, a clean integrity check and no broken foreign keys. The Mac checked the current and historical witness counts and historical content hashes. The primary had verified the signatures with its separate BWS credential. On the Mac, the dedicated `colima-cirra-standby` VM also ran an offline fixture review, read back the saved report and passed 12 workflow and audit-queue tests using the image ID pinned by production policy.

To rebuild the transfer, install `scripts/standby-snapshot.py` as a root-owned file at `/usr/local/libexec/cirra-standby-snapshot.py` on the primary and in the Mac's mode-0700 standby directory. Create a dedicated Ed25519 key in that directory. Authorize its public key on the primary with OpenSSH `restrict`, `from="<standby LAN address>"`, and the following fixed `command`. The existing operator account must be allowed to start this command.

```sh
/usr/bin/sudo -n /usr/bin/systemd-run --quiet --pipe --wait --collect \
  -p EnvironmentFile=/etc/lugos/restic-lugos-host.env -p EnvironmentFile=/etc/cirra-state-backup.conf \
  -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=read-only -p PrivateTmp=yes \
  -p UMask=0077 -p RuntimeMaxSec=10min /usr/bin/python3 -I /usr/local/libexec/cirra-standby-snapshot.py export
```

Create the private SSH config `source.ssh` in the Mac's standby directory. Define `Host cirra-standby-source` with the inventoried primary destination, operator user, `BindAddress` set to the approved Mac address, and the dedicated `IdentityFile`. Pin the primary's independently authenticated host key in a dedicated `UserKnownHostsFile`. Set `StrictHostKeyChecking yes`, `IdentitiesOnly yes`, `IdentityAgent none`, `BatchMode yes` and `ClearAllForwardings yes`. Configure the user's LaunchAgent to run the helper's `refresh` action on load and at a 3,600-second interval. Check its last exit status and compare the local snapshot ID to the primary's `last-verified.json`. FileVault unlock and the operator's login are required after a Mac reboot with this configuration.

The current public Tailscale Funnel terminates on `lugos-host`. A candidate public route is a dedicated Cirra hostname with a Cloudflare Tunnel connector on each serving machine. Both origins would acknowledge a delivery only after durable admission. A successful test must cover a primary failure during admission, a network partition and primary rejoin, with a single externally visible review per delivery. GitHub records failed webhooks and requires explicit redelivery. [Cloudflare Tunnel failover](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/) · [GitHub delivery failures](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)

The preferred worker order is `lugos-host`, then `intelmac`. Before enabling automatic promotion, the survivor must fence the old publisher or arbitrate ownership through an independent authority. Two machines alone cannot distinguish a partition from host loss. Both share the lab's network, so continued public admission during a site outage also needs an endpoint outside that failure domain.

Provision a clean isolated target and retrieve the restic and witness credentials from their independent escrow. Select the exact ID of a Cirra snapshot from before the loss. Create private, operator-owned scratch and receipt directories, load the approved repository environment into the recovery process, and run the following command. The original host and its local receipt files are unnecessary.

```sh
python3 -I /usr/local/libexec/cirra-backup-state.py verify \
  --snapshot "$SNAPSHOT_ID" --state /srv/lugos/state/cirra \
  --admin /recovery/control --scratch /recovery/isolated \
  --witness-file /recovery/escrow/witness.env
```

The command checks the tag, original source path, restored SQLite database and witness signatures. It prints the verified directory and records the drill separately from the scheduled recovery point. Compare the restored SQLite schema with the replacement release's supported versions before starting it. A signed receipt requires the original witness key even when a new publisher credential can be issued.

A replacement disk or a rollback to an older snapshot needs a cutover decision. Preserve the surviving state, quiesce every writer, and reconcile provider publication intents and work accepted after the snapshot before putting the replacement into service. Measure whole-host recovery separately through provisioning, credential recovery and service acceptance.

## Encryption of live storage

Off-host restic encryption covers the snapshots. The service database, receipts, SQLite journals and the nightly host archive also require protection on the active host. Verify the device backing each path and the swap configuration. A new encrypted volume can be staged beside the current filesystem, with protected key escrow and reboot-unlock testing. Verify a final consistent copy and preserve a lossless rollback path for post-cutover writes. Reformatting the existing disk, disposing of plaintext history, changing boot or swap, and choosing how an unattended host unlocks its storage require an explicit operator decision.
