# Recovering Cirra state

The recovery job takes a consistent off-host copy of the service database, durable queues, audit history, observer evidence and publication receipts. It then restores the entire snapshot to new private scratch and checks the recovered data. The restic repository must already exist on a separate host and have credentials available independently of the Cirra host.

## Install the recovery job

Install `scripts/backup-state.py` as the root-owned `/usr/local/libexec/cirra-backup-state.py` and the two `examples/systemd/cirra-state-backup.*` files in `/etc/systemd/system`. The installed script uses the system Python and its standard library. Review any change before replacing it. Keep the timer disabled until the first restore has succeeded.

The example reads restic's existing protected environment file and a separate root-owned, mode-0600 `/etc/cirra-state-backup.conf`. Set `CIRRA_APPROVED_BACKUP_REPOSITORY` there to the exact restic repository location approved for Cirra private state. The helper checks that it matches `RESTIC_REPOSITORY` before invoking restic. Keep restic credentials, the witness key and publisher or webhook credentials outside the state tree. Secure the offline configuration archive and its recovery passphrase under separate access controls.

The default state is `/srv/lugos/state/cirra`. The script includes this one directory, stays on its filesystem, and omits the operator scratch directories `acceptance` and `operator-recovery`. The service's configuration, secrets and other applications' state are outside the snapshot. The witness credential is selected by name from the service's protected configuration for local verification. Other installations can override the paths and witness variable using `--help`.

The service needs systemd, restic with `--retry-lock` and restore verification, access to the existing SFTP or storage credentials, and a Linux kernel that supports `tmpfs,noswap`. Verification restores to the service's private, non-executable tmpfs. Set its size for the uncompressed state and available RAM. The mount disappears when the service exits. The live state and backup snapshots remain intact. Multiple runs share a lock, and the snapshot also takes the deployment lock while work is paused.

Start and verify the first backup before enabling its timer.

```sh
sudo systemctl daemon-reload
sudo systemctl start cirra-state-backup.service
sudo cat /var/lib/cirra-recovery/last-verified.json
sudo systemctl enable --now cirra-state-backup.timer
```

The timer runs daily at 06:30 UTC and catches a missed scheduled run after boot. A failed run retries after 15 minutes, up to the configured start limit. Compare the time of the last verified snapshot with the service's recovery-point target. An encrypted restic snapshot on a separate LAN host protects against loss of the service disk. A site disaster requires an independently approved off-site copy.

## Snapshot and verification

1. The helper checks both queues for running jobs before and after stopping writers. Malformed records that the scheduler cannot run remain in the snapshot for diagnosis. A filesystem read error prevents the backup from proceeding. The helper pauses the timer, receiver, optional audit-read service and worker only when originally active and waits for health notifications to finish. Avoid CLI writes against the same state during this short window.
2. Restic snapshots the configured state directory with the `cirra-state` tag. The helper restarts the originally active units before beginning restoration. After an interruption, the backup and systemd's `ExecStopPost` take the shared deployment lock before restarting any unit from the saved journal. An active deployment keeps exclusive control. The journal stays intact until the next service retry can resume those units, even if the backup repository is unavailable.
3. The helper verifies the full snapshot ID, tag and source path, then calls `restic restore --verify` into a new directory. It runs SQLite's integrity and foreign-key checks using a read-only connection, verifies the expected Cirra tables, and checks all current and historical observer signatures using the separate witness credential. Historical receipts must also match their content-addressed filenames. Restored code and provider actions are never executed.
4. Root-only receipts under `/var/lib/cirra-recovery/` contain the backup identity, quiescence time, verification time, schema version and witness counts. `pause_seconds` covers the backup and return of the service units. `restore_verify_seconds` measures restoration and validation from Nodens or the configured repository. `restore_point_age_seconds` measures the age of the captured state at the end of that drill. The interval since the last verified capture determines exposure at a later failure.

## Reviewing off-host retention

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
