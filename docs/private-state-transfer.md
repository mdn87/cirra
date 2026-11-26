# Private backups on the two data hosts

With a dedicated encrypted restic repository on Nodens and separate access for each data host, the elected Cirra worker can protect its complete private state. Direct access lets the Intel Mac restore during a primary outage. As of November 26 the shared backup repository accepts connections from the primary. Dedicated two-host access awaits the operator’s decision. Each data host needs its own authorized storage access, an independently recoverable repository password, the configured witness signing key and private local scratch. The hourly ZIP pull from the primary uses a different format and depends on the primary's export service. [Coordinated service configuration](coordinated-service.md) · [Existing recovery arrangements](state-recovery.md)

## Run the backup

Set `STATE` to the local service directory or managed selector. Set `LOCAL_HOST` to the `local_data_host` digest from this machine's `cirra coordination-status`. The repository environment contains `RESTIC_REPOSITORY` and the dedicated storage-role settings. The separate approval file contains `CIRRA_APPROVED_BACKUP_REPOSITORY` with the same destination. Use private, owner-readable files outside the state tree. SFTP and HTTPS REST repositories are supported. Keep the existing SSH host-key or TLS verification for the approved destination.

```sh
python -m cirra.private_transfer backup --state-dir "$STATE" \
  --expected-host "$LOCAL_HOST" --repository-env "$REPOSITORY_ENV" \
  --approval-file "$APPROVAL_FILE" --witness-file "$WITNESS_ENV"
```

The helper requests a capture every six hours of active ownership. The elected worker takes its capture at a completed or idle pass, holds the review and audit intake locks, and checks its lease and the private authority. The repository agent uses its own lock, sends the capture and downloads the exact encrypted snapshot into new scratch. It checks every file digest, the signed manifest, SQLite integrity, foreign keys and schema, alongside all current and historical witness signatures. Only the worker can install `data-backup.json` after this independent restore. The source still needs the same elected host and epoch. An interrupted upload resumes by its capture tag.

These two processes use the same unprivileged OS account with distinct credential feeds. The worker needs its configured witness reference. Only the repository agent needs the storage credentials. Provision a mode-0700 sibling called `<resolved-state-directory>.private-transfer` owned by the service user, and grant both services access to this directory. It holds a temporary plain archive and the verification scratch. The Mac uses local FileVault storage. Linux can use a private, appropriately sized non-swappable filesystem for this spool. Leave space for the captured archive, the downloaded archive and the restored tree. Ordinary backups change no repository retention settings.

On Linux, a dedicated systemd backup service can use `LoadCredential=witness:/run/cirra-backup-witness.sock` with the existing root-only witness provider at its installed socket path. The manager obtains the raw credential and makes it readable by this service. Pass `--witness-credential %d/witness` to the helper. Scope the repository role and its private configuration to this service's credential namespace. The worker unit can limit its writable paths to its state and the shared spool. The existing protected witness environment file and same-owner private Unix socket are alternatives when running under their authorized identities. [systemd credential handling](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#LoadCredential=)

## Schedule the Intel Mac

Use the deployed interpreter and paths on its local FileVault volume. Render the LaunchAgent, lint it and load it in the authorized operator's active login session. The generated plist contains paths and the local opaque host identity. Store the separate witness and storage credentials under their existing access controls.

```sh
job="$HOME/Library/LaunchAgents/foo.newman.cirra-private-backup.plist"
umask 077
python scripts/macos-private-backup-plist.py --python "$CIRRA_PYTHON" \
  --state-dir "$STATE" --expected-host "$LOCAL_HOST" \
  --repository-env "$REPOSITORY_ENV" --approval-file "$APPROVAL_FILE" \
  --witness-file "$WITNESS_ENV" --logs "$PRIVATE_LOG_DIR" > "$job"
plutil -lint "$job"
launchctl bootstrap "gui/$(id -u)" "$job"
launchctl print "gui/$(id -u)/foo.newman.cirra-private-backup"
```

The job runs at load and checks every 15 minutes while this login session is active. It emits `status` and a UTC `next_check`. A passive host reports `standby`. A current source reports the next six-hour capture deadline. `waiting_for_worker_capture` and `waiting_for_worker_receipt` identify the unfinished step and retry on the next launch. Repository or verification failures exit nonzero and preserve the previous verified recovery point. Direct access to Nodens and the witness credential must be available locally before relying on Mac-originated backups.

## Fetch or transfer a recovery point

The automated standby uses the quorum-selected donor. An operator can also recover directly from the same approved encrypted repository while the donor is unavailable. Set `DONOR` from the current quorum authority and choose a fresh private filename. The fetch selects the newest capture for that donor, verifies the complete archive in scratch and publishes the new file exclusively.

```sh
python -m cirra.private_transfer fetch-latest --expected-host "$DONOR" \
  --archive "$NEW_ARCHIVE" --repository-env "$REPOSITORY_ENV" \
  --approval-file "$APPROVAL_FILE" --witness-file "$WITNESS_ENV"
cirra install-standby-snapshot --service-config "$CONFIG" \
  --state-dir "$MANAGED_SELECTOR" --archive "$NEW_ARCHIVE"
```

The repository tag is `cirra-private-authority-v1`, the restic hostname is the donor digest and the single source path is `/cirra-private-transfer.zip`. The restic snapshot time is the signed capture time in UTC. The full restic ID identifies encrypted storage. The independent signed manifest digest is the `snapshot_id` used in Cirra's receipts. Fetch requires a capture less than 24 hours old and writes only the new archive. Restoring an archive also verifies it and writes its original five-field `data-restored.json` into an absent or empty target. File contents, modification times, ordinary permissions and empty directories survive transfer. A restored copy cannot renew the original donor's capture timestamp.

For an operator-directed return to the primary, first freeze the elected worker for the exact destination with `cirra freeze-coordination`, then capture the frozen source. Transfer the signed archive through the authorized private channel and use the same standby installer on the destination. The installer checks the frozen tip against quorum.

```sh
python -m cirra.private_transfer capture --state-dir "$STATE" \
  --archive "$NEW_ARCHIVE" --witness-file "$WITNESS_ENV"
```
