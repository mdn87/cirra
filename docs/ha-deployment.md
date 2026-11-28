# Deploy and roll back the enrolled HA installation

Production runs on `lugos-host` and `intelmac`. Both receive webhooks through
`cirra-hooks.newman.foo`; quorum permits one worker to mutate private state.
The managed state selectors are `/srv/lugos/state/cirra-ha/current` on Linux
and `~/.local/share/cirra-standby/state/current` on the Mac.

Use `scripts/deploy-ha.py` from a trusted checkout with the configured operator
SSH identities. It targets these two hosts only. The standalone
`deploy-release.sh` refuses this topology. No database migration, enrollment,
quorum reset, credential change, or retained-state deletion is part of a release.

## Stage and check

Commit and validate the release, land its PR, then use its full immutable SHA:

```sh
python scripts/deploy-ha.py stage --host lugos-host --sha <40-character-sha>
python scripts/deploy-ha.py stage --host intelmac --sha <40-character-sha>
python scripts/deploy-ha.py preview --host lugos-host --sha <40-character-sha>
python scripts/deploy-ha.py preview --host intelmac --sha <40-character-sha>
```

Staging verifies the Git archive hash and installs hashed dependencies into a
release-local virtual environment. Existing releases are reused only with a
matching installation receipt. A partial installation is retained for inspection.
Preview checks the live SQLite schema and queue format without writing them.
Any schema change requires a separate migration plan.

Runtime paths resolve through `<service-root>/ha-current`. The transaction
updates the installed HA launchers, worker/webhook services, audit-reader mount,
health runtime, backup runtime, and donor-refresh runtime. Existing confinement,
credentials, state selectors, and voter membership remain in place.

An observer implementation change requires a new policy definition digest. The
tool retains the original policy as `intake-<route>.yaml`: admission uses that
original identity while execution uses the new observer pin. Compatibility is
limited to the `cirra/isolated-tests` definition digest; changing model, quotas,
repositories, check source, or other policy fields is refused. The quorum's
existing policy keys and daily counters are never reset.

## Roll out with one healthy origin

First verify a fresh recoverable backup and that Linux holds authority. If the
Mac is active, use the existing coordinated handback procedure first. Stop the
Mac standby, then drain Linux; webhook receivers remain running:

```sh
python scripts/deploy-ha.py worker-stop --host intelmac
python scripts/deploy-ha.py worker-stop --host lugos-host
python scripts/deploy-ha.py activate --host intelmac --sha <40-character-sha>
python scripts/deploy-ha.py activate --host lugos-host --sha <40-character-sha>
```

Each activation verifies the other origin's health and that both workers are
paused. It waits for scheduled backup/refresh work to finish, pauses their
schedulers during the switch, then restores the schedules. A host-local lock
rejects concurrent operators. The saved transaction contains the exact previous
files and pointer, and remains private on that host. Startup failure restores
the previous runtime and configuration automatically. Workers remain paused
until the operator resumes them.

If an inspected branch CI job requires reconciliation, use
`requeue-coordinated-branch.py` under the native role environment while both
workers are paused. It checks the exact job digest, original data authority,
selector, lease and worker lock. It preserves history and daily budget usage;
it cannot retry an uncertain model review.

```sh
python scripts/deploy-ha.py worker-start --host lugos-host
python scripts/deploy-ha.py worker-start --host intelmac
python scripts/deploy-ha.py probe --host lugos-host
python scripts/deploy-ha.py probe --host intelmac
```

Check running process revisions, observer pins, Linux `cirra health`, native
Git check readback, the public `/healthz`, a fresh verified backup, and the Mac
donor-refresh receipt. Health includes unresolved failures regardless of age;
a green receiver probe alone does not establish worker or backup health.
Once the HA route is verified, disable and stop the unused
`cirra-webhook.service` on port 8796. Keep the original `cirra-worker.service`
fenced and stopped. Pending session audits require explicit import or disposition;
deployment does not cancel or accept them.

## Rollback

Save the transaction ID returned by each activation. Pause both workers in the
same order, then roll back one host at a time:

```sh
python scripts/deploy-ha.py rollback --host intelmac --transaction <transaction-id>
python scripts/deploy-ha.py rollback --host lugos-host --transaction <transaction-id>
```

Rollback requires a healthy peer, checks that the old release supports the
current schema/queue format, restores the saved files and pointer, and verifies
receiver readiness. It does not revert private state or quotas. Files introduced
by the release are retained inside the deployment journal. Resume workers and
repeat the operational checks. If a transaction was interrupted, inspect its
on-host `ha-deployments/<id>/transaction.json` and roll it back before activating
another release; do not discard its journal.
