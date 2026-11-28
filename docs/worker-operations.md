# Worker operations

The operator who owns the Cirra service on `lugos-host` is Matt. The five-minute
`cirra-health.timer` sends failures through Lugos operator attention. Use
`cirra health --state-dir "$STATE"` for the current alert and
`cirra jobs --state-dir "$STATE"` for the queue, worker, and publication records.
On `lugos-host`, `STATE=/srv/lugos/state/cirra-ha/current`; on Intel Mac,
`STATE=/Users/matt/.local/share/cirra-standby/state/current`. Resolve the current
authority before changing either queue. Linux runs `cirra-ha-worker.service`
and `cirra-ha-webhook.service`; the original worker remains fenced. Use the
[HA deployment and rollback procedure](ha-deployment.md) for this topology.

## Service objectives

| Indicator | Calculation | Objective |
| --- | --- | --- |
| Webhook queue admission | Successful new submissions plus acknowledged duplicates divided by all eligible submissions in the UTC day. Use the webhook journal for the complete indicator. | At least 99%. |
| Useful result within 30 minutes | Accepted webhook changes reaching `done` within 30 minutes divided by accepted changes at least 30 minutes old, accepted in the trailing 24 hours. See `eligible_webhooks_24h`, `completed_within_30m`, and `on_time_percent`. | At least 95%. |

These objectives allow up to 1% of eligible submissions to be refused and up to
5% of eligible changes to miss the 30-minute target. When a measured window
exceeds either allowance, Matt defers feature releases affecting that path and
prioritizes repairs. Recovery and security fixes can proceed. Resume routine
releases when both affected indicators meet their targets over their stated
windows with eligible traffic. Record the counts and recovery time in the incident.

Admission counts authenticated requests for an enabled event from a trusted author
that reached the queue. The journal records `admission` as `accepted`, `duplicate`
or `refused`, including eligible submissions that encountered a lock or storage
failure. Count HTTP 202 with an accepted or duplicate outcome in the numerator.
The `admissions_today` and `acceptance_percent_today` metrics give the durable
queue's counts for new submissions, duplicates and capacity refusals. Authentication
failures, conflicting delivery identities, ignored events and reads are outside
the objective. Inspect the proxy separately for connections lost before the
receiver can classify a request.
Success on the second indicator means a usable result
was saved, including an actionable failing check. Closed or draft changes that
the worker skips, manual and reconciliation requests, and human audit sessions
are excluded. Admission counters begin on the first delivery after upgrade.
An empty denominator produces JSON `null`. Counts of arrivals
and completions over the last 24 hours reveal volume and changes in throughput.
Rerunning a terminal delivery explicitly updates its current completion record.

This command calculates admission from the current UTC day's webhook journal on the host.

```sh
journalctl -u cirra-ha-webhook.service --since "$(date -u +%F) 00:00:00 UTC" -o cat |
  jq -Rn '[inputs | fromjson? | select(.event == "webhook_request" and .admission != null)] |
    if length == 0 then null else
      100 * (map(select(.status == 202 and (.admission == "accepted" or .admission == "duplicate"))) | length) / length end'
```

The worker publishes the configured slot limit with its heartbeat. Admission
reclaims completed work under pressure. Unresolved jobs requiring attention and
unreadable records occupy a slot. The health timer alerts at 10% remaining
capacity, on a refusal in the past 15 minutes, or after eligible work has waited
over 30 minutes. Provider cooldowns delay eligibility until the stored reset
time. The disk reserve is the smaller of 1 GiB and 5% of volume capacity.

## Incident response

| Impact | Owner and response |
| --- | --- |
| Multiple routes are refusing deliveries, the worker is unavailable, or required checks cannot complete | Matt acknowledges the operator-attention alert within 15 minutes, identifies affected repositories, and informs their maintainers of merge impact. Provide an update every 30 minutes until service recovers. |
| One route has credential trouble, repeated audit-publication failures, or sustained backlog | Matt inspects the affected job and the named route within one hour. Escalate to its repository or provider administrator for access or quota changes. Promote to the first row if more routes fail or a release is blocked. |
| One window misses an objective without a current outage | Matt reviews arrivals, refusals, and completions the next working day and records any recurring cause. |

Look up the webhook journal's `request_id` or provider delivery ID. An
acknowledged enqueue includes its durable `job_id`. The worker records that ID,
the route, attempt timestamps and duration, result, and provider ID where
available. Receiver and audit records carry elapsed time in milliseconds.
An audit has a pass ID and, once selected, its job and attempt IDs.
These identifiers suffice for an incident update. Credentials, webhook bodies,
and command output belong outside incident messages.

Provider quota refusals before execution return the daily admission and defer
other jobs using the same configured reader until reset. A permission denial
goes to attention and pauses the reader for 15 minutes. Correct the credential or
target and inspect the saved job. On standalone state, use `cirra requeue` with
a reason. For a coordinated branch-CI job, pause both workers and run
`scripts/requeue-coordinated-branch.py` on the original authoritative host with
the inspected job's digest and a reason. Resume that worker before the standby.
This preserves history and the existing quota sequence. An interrupted branch
observer can recover under a fresh quota charge; uncertain model-review
executions still require their original execution to be reconciled.
SQL and JSON records for a completed PR audit survive an interrupted check
publication. The worker retries up to five times with increasing delays and
honors provider reset times. If a create may have reached GitHub, recovery reads
the check by App, head and audit identity and verifies the recorded result.
Once the provider recovers, restart publication with
`cirra retry-audit-publication --state-dir "$STATE" --job "$AUDIT_JOB" --reason "$REASON"`.
This uses the completed audit and its original admission. If GitHub continues
to return no matching check after an uncertain create, the job returns to
attention for investigation. Close the incident when health is green, a fresh
delivery reaches its expected result, and the affected repositories have been updated.

## Storage retention

The worker counts large pre-upgrade queues in batches and archives old jobs in
the background. Admission under pressure reads at most four indexed candidates
and archives at most one terminal job. A large pre-upgrade queue can briefly
return 503 for new admissions while its initial count finishes. Previously
acknowledged deliveries continue to deduplicate.

The first new writer records the required file-backed queue format. The release
driver checks it before deployment, explicit rollback and automatic recovery.
Managed worker and webhook systemd drop-ins also run this check before startup,
including when an older operator script switches the release symlink. A rollback
refusal preserves the queue, its archived replay keys and outstanding publication
intents. Select a release that reads the recorded format or roll forward with a
fix. Restoring an older full-state backup requires reconciling provider writes and
deliveries since that backup. SQLite remains at schema version 3.

The review queue moves successful or skipped records to `queue/archive/` after
seven days, or earlier under pressure. Audit maintenance archives completed,
superseded or cancelled requests after seven days. Unresolved attention and
failed jobs stay visible until explicitly resolved, including records archived
by an older release. Health has no age cutoff for these failures by default;
`--attention-window` is an explicit historical filter, not an operational default.
Active coordinated hosts also report missing or invalid backup receipts and
backups older than 23 hours, with one hour to capture a new authority's first
backup. Current receipts and
compact delivery and completed-payload keys stay on the service volume for
the lifetime of the installation. Restore these from the same backup as the
worker state after disk recovery.

During a monthly maintenance window, use the existing protected backup storage
or an operator-selected encrypted volume with adequate space for cold evidence.
Choose a **new** directory for each batch so multiple versions remain recoverable.
Keep 30 days on the service volume. The selector covers terminal queue archives,
immutable receipt history, source archives and Git objects of completed
observations, and closed audit-agent logs whose checkouts have been removed.
Current signed witnesses, observer container logs and the SQLite store
stay online. Retain cold evidence according to the repository's recovery policy.

On the Linux host, pause the worker and webhook during final transfer. Set
`ARCHIVE` to the new directory on the backup volume and `MANIFEST` to a
protected temporary file. Confirm the destination mount and sufficient space.
The following commands make a list, copy its exact files, then verify checksums.

```sh
cirra list-cold-evidence --state-dir "$STATE" --older-than-days 30 > "$MANIFEST"
rsync -a --from0 --files-from="$MANIFEST" "$STATE/" "$ARCHIVE/"
rsync -anic --from0 --files-from="$MANIFEST" "$STATE/" "$ARCHIVE/"
```

Proceed only if both transfers exited zero and the checksum comparison printed
no differences. This final pass removes source files only after rsync confirms
they exist at the destination. Keep the manifest with the batch and restart
the services.

```sh
rsync -ac --remove-source-files --from0 --files-from="$MANIFEST" "$STATE/" "$ARCHIVE/"
```

Restore an archived queue record under its original relative path before
explicitly requeuing it. Redelivered webhook payloads still match their compact
completion keys while the original record is in cold storage.
