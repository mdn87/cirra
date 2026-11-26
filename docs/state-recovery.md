# Recovering Cirra state

The recovery job takes a consistent off-host copy of the service database, durable queues, audit history, observer evidence and publication receipts. It then restores the entire snapshot to new private scratch and checks the recovered data. The restic repository must already exist on a separate host and have credentials available independently of the Cirra host.

Matt set two requirements on November 26, 2026. Losing the service host may cost at most 24 hours of state, and Cirra should keep serving through that loss. The daily verified snapshot supports data recovery. At a 24-hour interval it has no margin for a delayed or failed capture. The current single-host deployment still interrupts service during backup and host loss. Continuous service requires an independent ingress path, durable admission on another host and a single authoritative writer for review and publication.

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

The recovery unit runs as root. It opens a root-owned, mode-0600 local Unix socket before pausing any service. Systemd starts the BWS lookup under the service account for that connection. The witness helper checks the client's kernel-reported UID and sends the key over the socket. The root process holds it in memory for verification. Restic and systemctl receive their usual environment. The independent stop hook can resume services even when BWS is unavailable.

Install the helpers and unit files before starting the first backup. The Python helpers use the system standard library. Keep the installed recovery and health helpers in a root-owned directory.

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

The timer runs daily at 06:30 UTC and catches a missed scheduled run after boot. A failed run retries after 15 minutes, up to the configured start limit. An encrypted restic snapshot on a separate LAN host protects against loss of the service disk. A site disaster requires an independently approved off-site copy.

The five-minute health check alerts if verification evidence is missing or unreadable, the backup service reports failure, or the last verified capture exceeds the 24-hour data-loss limit. A failed run records a private timestamp under `/var/lib/cirra-recovery`. Starting a retry or verifying an older snapshot leaves the alert active. A newly captured and successfully restored backup clears the backup problem. The normal alert resolves once the worker and backup checks both pass.

Only the root-owned health helper reads the recovery receipt. The health journal and the operator event receive a short reason without the snapshot ID, repository location or credentials. Inspect `journalctl -u cirra-state-backup.service` locally for the failed run. A host without scheduled backups uses the ordinary health service without the drop-in.

## Snapshot and verification

1. The helper checks both queues for running jobs before and after stopping writers. Malformed records that the scheduler cannot run remain in the snapshot for diagnosis. A filesystem read error prevents the backup from proceeding. The helper pauses the timer, receiver, optional audit-read service and worker only when originally active and waits for health notifications to finish. Avoid CLI writes against the same state during this short window.
2. Restic snapshots the configured state directory with the `cirra-state` tag. The helper restarts the originally active units before beginning restoration. After an interruption, the backup and systemd's `ExecStopPost` take the shared deployment lock before restarting any unit from the saved journal. An active deployment keeps exclusive control. The journal stays intact until the next service retry can resume those units, even if the backup repository is unavailable.
3. The helper verifies the full snapshot ID, tag and source path, then calls `restic restore --verify` into a new directory. It runs SQLite's integrity and foreign-key checks using a read-only connection, verifies the expected Cirra tables, and checks all current and historical observer signatures using the separate witness credential. Historical receipts must also match their content-addressed filenames. Restored code and provider actions are never executed.
4. Root-only receipts under `/var/lib/cirra-recovery/` contain the backup identity, quiescence time, verification time, schema version and witness counts. `pause_seconds` covers the backup and return of the service units. `restore_verify_seconds` measures restoration and validation from Nodens or the configured repository. `restore_point_age_seconds` measures the age of the captured state at the end of that drill. The interval since the last verified capture determines exposure at a later failure.

## Reviewing off-host retention

The verified receipt records total and free bytes on the restore filesystem while the restored copy is still present. The five-minute health check raises a capacity alert when that copy leaves less than 5% free, capped at a 1 GiB reserve. On lugos-host the service uses a 1 GiB private tmpfs. If it approaches the reserve, check host memory headroom and increase the `size=` setting in the backup unit's `TemporaryFileSystem` mount. Reload systemd and confirm the next verified receipt shows the new capacity. Preserve the `noswap` mount option when resizing.

The daily job keeps its tagged Cirra snapshots in Nodens. During monthly maintenance, Matt can list the oldest and newest recovery points with `restic snapshots --tag cirra-state` and check free space with the storage owner. Record the latest successful restore alongside those figures. The host archive's 14-copy rotation applies to a separate set of local archives. The Cirra job preserves the off-host snapshots. Any later expiration of those snapshots requires a retention decision for Nodens.

## Restoring service after host loss

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
