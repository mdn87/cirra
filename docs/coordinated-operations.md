# Operator commands on enrolled HA state

Use the release's `python -m cirra.coordinated_ops` or
`scripts/operate-coordinated.py` for the five supported operator actions:
`request-audit`, `cancel-audit`, `retry-audit-publication`, `plan-release` and
`dispatch-release`. The ordinary CLI continues to refuse direct writes to
enrolled state. This entry point does not authorize a waiver or release;
use it only for the operator-approved action.

## Invocation and ownership

Follow [HA deployment](ha-deployment.md) to pause the standby and then drain
the active worker. Keep receivers and voters running. Invoke the command on
the original authoritative host, using its normal private role environment
and its configured `ha-current` Python. Pass `--service-config` before the
command; the remaining flags match that command's ordinary CLI contract.

```sh
python -m cirra.coordinated_ops --service-config /path/to/service.yaml \
  request-audit --state-dir /path/to/state/current \
  --config /path/to/policy.yaml --tier full --head <full-sha> --ref main

python -m cirra.coordinated_ops --service-config /path/to/service.yaml \
  plan-release --state-dir /path/to/state/current \
  --config /path/to/policy.yaml --run <review-run-id> \
  --workflow-id <trusted-workflow-id> --ref main --output /private/request.json
```

`request-audit`, `plan-release` and `dispatch-release` require a policy whose
digest matches an enrolled route, and the audit or release repository must
match that route. Planning always requires `--config`, including
when no waiver is requested. `dispatch-release` retains its existing request,
GitHub App, reader-token and provenance flags. Credentials are injected by
the existing role launcher; never put secret values in command arguments.

The entry point validates the managed selector, existing schema, enrollment
and original unfrozen authority before acquiring the native quorum owner.
It then rechecks authority and selection, maintains the lease, runs one
allowlisted native handler against the resolved physical version and
checkpoints the result. It never promotes a standby or refunds budgets.
The worker filesystem lock excludes worker/backup/transfer writers; queue
operations take their own native locks. Restart the active worker first,
then the standby, and verify health and backup freshness.

## Outcomes and recovery

| Outcome | Meaning and next action |
| --- | --- |
| `complete`, exit 0 | The native handler returned success and its authority checkpoint was confirmed. For dispatch, also inspect the nested provider result: `accepted` is an acknowledgment, not an observed workflow completion. |
| `refused`, exit 1 | Admission failed before the native handler started. Resolve the stated prerequisites before retrying. |
| `failed`, exit 1 | The native handler refused or failed after admission. It may have made local changes; those changes were checkpointed. For example, planning may enqueue a missing release audit before refusing the gate. Inspect state and use the same operation identity. |
| `uncertain`, nonzero | A native external outcome cannot be established. Use `dispatch-release --reconcile`; do not send again or delete receipts. |
| `outcome_unconfirmed`, exit 2 | The handler may have committed locally or sent externally, but the authority checkpoint was not confirmed. Inspect the current authority. Repeat local actions only on that host using the original IDs; reconcile an external dispatch. Never copy the superseded tree back over current state. |

A request file alone is not a success receipt or a publication grant. The
wrapper emits its outer receipt only after attempting the authority checkpoint.
It preserves native command results inside `result` and the native exit code
in `operation_exit_code`.

`request-audit` attaches to an identical queued/completed identity; admission
budgets are charged later by the worker's existing one-use mechanism.
`cancel-audit` keeps native cancellation and publication guards.
`retry-audit-publication` schedules the native worker reconciliation path for
an eligible completed audit; it does not publish directly or rerun its audit.

## External release safety

Before a coordinated workflow POST, the native release code reserves an
opaque publication slot on quorum using the stable request ID and trusted
workflow revision. It writes its local uncertain intent and checks the lease
again before sending. A lost response or lease leaves the quorum reservation
in place even if the next host restores a snapshot from before the POST.

Every coordinated dispatch first checks native workflow runs. A single run
with the exact request title, workflow, target branch and dispatch event can
be reconciled; its commit must contain the reviewed candidate. The native run
ID and fingerprint confirm the quorum reservation. Multiple matches are
refused. A missing/hidden run cannot clear an existing reservation and cannot
authorize a second POST. An accepted 204 acknowledgment leaves the reservation
pending until native readback. This is conservative at-most-once submission,
not a guarantee of eventual dispatch after an uncertain pre-send failure.

The coordinated wrapper refuses `--retry-uncertain`; restoring older private
state is never permission to repeat a send. Keep the original request ID,
use `--reconcile`, and inspect provider evidence if visibility remains unknown.
The fence supplements existing fresh-CI, audit, merged-candidate, protected-ref
and native receipt checks. It does not make quorum and local SQLite/file writes
one atomic transaction.
