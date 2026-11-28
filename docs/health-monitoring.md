# Health and recovery monitoring

Cirra exposes different checks for accepting deliveries, processing work and
maintaining a recoverable private state. A successful receiver probe alone does
not prove that an audit has finished or that another host can take over.

## Public receiver readiness

`GET /healthz` on the webhook receiver answers HTTP 200 when the receiver can
confirm admission readiness, and HTTP 503 with a generic status when it cannot.

In standalone mode, the receiver checks the bounded queue-slot count and probes
writes in the state, queue and existing delivery-receipt directories. It waits at
most 0.2 seconds for the admission lock. It does not enqueue a test job, scan all
job records or reclaim completed work. A full queue, pending legacy census,
invalid capacity record or failed storage probe produces 503. A later successful
check returns 200 without restarting the receiver.

Coordinated mode keeps using the shared admission check. Its receiver can accept
work into quorum while its local worker is on standby or paused. Monitor the
worker and recovery evidence separately.

## Worker health and audit warnings

`cirra health --state-dir <selected-state>` returns JSON. `healthy` and `problems`
continue to determine its exit code: 0 for healthy, 1 for an operational problem.
The additional `warnings` array does not change the exit code. Existing audit
failures and unreadable queue records remain operational problems.

Audit requests in `awaiting_import` or `deferred` warn when their original request
is more than 48 hours old. `audit_metrics.waiting` reports each state's count and
oldest request age. A worker poll that updates the record does not reset that
age. Cancelled or superseded work is excluded. The metric measures request age,
not the exact time spent in the current state.

Supply `--service-config <service.yaml>` to evaluate repository full-audit ages
against each configured route's `audit.full.max_age_days`. A missing or overdue
baseline produces an `audit-baseline` warning. Structural full audits of a pull
request do not replace the repository baseline. If two routes describe the
same repository, the stricter configured age is used.

Without service configuration, existing baseline ages are still reported, but
`baseline_policy_known` is false and `overdue` is null. No policy expiry is
inferred. The health check opens existing audit state read-only and never creates
or upgrades a database for this visibility. An unreadable baseline or a bounded
query limit produces a warning rather than a false service outage.

Use the availability exit code for an operational heartbeat. Route audit warnings
to a separate work-status notification if desired; they are not an empty-queue
release requirement.

## Backup and standby freshness

On a coordinated active worker, a missing, mismatched or 23-hour-old backup is an
operational problem. A new authority retains the existing one-hour grace period
to replace its donor's backup. A frozen transfer is checked through coordination.

On a coordinated standby worker, a missing, mismatched or 23-hour-old verified
restore is an operational problem. The restore must match the local donor
authority's host, epoch and tip and contain a valid snapshot identifier and
timezone-aware capture time. A restore more than one minute in the future is
also refused. These checks alert before the 24-hour promotion freshness limit.

`recovery_metrics` reports the worker state, the 23-hour warning threshold and
each local backup/restore capture time and age. The active role requires its
backup; the standby role requires its restore. The other receipt can belong to a
previous tenure and its age alone is not an availability verdict. Local health
does not independently prove the donor still owns quorum or that an external
notification arrived.

Run health monitoring on both data hosts and route alerts through a path that
survives loss of either host. Monitor receiver reachability and missing host
heartbeats externally as well: a stopped host cannot emit its own failed check.

## Independent heartbeat publisher

`scripts/report-health.py` runs the native health check and sends its result to
Uptime Kuma directly. Each host has its own push monitor. A separate shared
active-worker monitor receives updates only from a worker reporting `running`
with a current heartbeat and no coordination failure. A standby or stale running
record never refreshes that shared monitor.

```text
python scripts/report-health.py --state-dir <selected-state> --service-config <service.yaml> --host-heartbeat-file <private-host-url-file> --active-heartbeat-file <private-active-url-file>
```

Each credential file contains one base URL ending in `/api/push/<token>`, without
query parameters or a fragment. Keep it private (mode 0600 or 0400 on Unix).
The Linux service also accepts systemd's root-owned 0440 ACL credentials,
only inside its declared `/run/credentials/<unit>.service` mount. Ordinary
group-readable URL files remain refused.
Symlinks, nonregular files, oversized files, URL credentials and redirects are
refused. HTTPS is supported with normal certificate verification; plain HTTP is
limited to literal RFC1918 or loopback addresses. Secret URLs and receiver error
bodies are never logged. No automatic retry can amplify a failing receiver.

The publisher sends `status=up` or `status=down` and requires the receiver's
successful acknowledgement. This follows [Kuma's push endpoint](https://github.com/louislam/uptime-kuma/blob/master/server/routers/api-router.js).
The default network timeout is five seconds per update, including a response-body
deadline, with a 4 KiB response limit. A health or notification failure produces
a nonzero exit code and a sanitized result. Audit warnings alone still send `up`.
An unavailable native health check sends the host monitor `down` and leaves the
shared active monitor untouched.

The site examples are `examples/launchd/foo.newman.cirra-health.plist` (every five
minutes) and `examples/systemd/ha/cirra-health.service.d/zzz-kuma-push.conf` (using
the existing Linux timer). Linux reads the URL files through systemd credentials;
the Mac reads private files under its HA configuration directory. Configure
Kuma's missing-heartbeat window longer than the five-minute schedule and verify
both notification and recovery delivery before counting the monitors as live.
The publisher does not depend on lugos-host's local event-ingest service.
