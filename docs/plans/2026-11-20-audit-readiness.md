# Production audit readiness

Assessed 2026-11-20 at 05:45 UTC against local `d9f6d9a` and the live
Cirra service. This closes the readiness investigation, not the production
audit rollout. The October recovery handoff is historical evidence.

## Observed state

| Area | Evidence | Consequence |
| --- | --- | --- |
| Deployed code | The current release resolves to `65438cc21b99d752da37aceb3747583874895597`. | Later local changes are not deployed. |
| Service processes | Worker and webhook are active, each with zero restarts; the worker heartbeat is current. | The worker is running, but process health alone does not establish provider access. |
| Application health | `cirra health` exits 1: GitLab reconciliation reports HTTP 401. The health alert marker exists. Receiver health returns HTTP 200. | Restore provider access before claiming the service is healthy. |
| GitHub audit policy | Only `pr` is enabled, with the session runner and a `critical` gate. | Full and release audits are inactive. |
| GitLab audit policy | No audit tiers are configured. | GitLab audit publication is also unsupported by the current implementation. |
| Audit queue | Three PR jobs are `awaiting_import`, for changes 24, 25 and 28. | The old handoff's count of two is stale. Session audits need an explicit report import. |
| Stored audits | Two completed PR attempts for change 23; three prepared attempts and two superseded attempts. No full or release rows. | There is no stored repository baseline or release audit. |
| Audit read service | The systemd unit is not installed. | The read-only route implemented after the deployed release is unavailable. |

The waiting attempt IDs are `7c0591d427884cbb8d71e44c5c61f25c`,
`8637def5475f4512bd29739d41b0f07f`, and `5d0b6699773a4982870a946daa874266`,
respectively. Local merge history records all three changes as merged. That
does not settle their audits or authorize manufacturing reports for them.

Inspection used the inventoried operator SSH identity, selected systemd
properties, the health and audit-jobs commands, allowlisted policy fields,
and SQLite opened with `mode=ro`. The worker launches through Glas. No secret
values were printed, and no credentials, jobs, service configuration or
database schema were changed. Sanitized inspection evidence is retained
locally under `.lugos-audit/audit-readiness/live-readiness.json`.

## Next step: recover GitLab reader access

Trace the configured reader reference through the Glas launch into a bounded
provider read. Determine whether the 401 comes from expired or revoked
credentials, incorrect injection, or repository access. The observed 401 does
not distinguish those causes; this assessment did not inspect secret values
or test a replacement credential.

Success requires a successful GitLab reconciliation, healthy application
status, and resolution of the pending health alert. Keep the route enabled;
disabling it would hide lost coverage. Credential replacement or rotation
requires the operator's authorization. No such action was attempted here.

## Audit activation after recovery

1. Inspect the three pending packets and decide whether each still warrants
   an audit of its pinned revision. Import valid reports or explicitly cancel
   obsolete work with a recorded reason; do not treat a merged PR as audited.
2. Prove one repository-scoped full audit with the session runner. Preserve
   the previous policy for rollback, enable `full`, observe baseline queueing,
   produce and import a report matching its packet, and verify baseline use
   by a subsequent PR audit. Full audits must remain non-blocking.
3. Prepare the separate release demonstration: a protected target, a merged
   candidate with witness-verified CI definition evidence, a trusted native
   workflow and the restricted Actions credential. Enable `release`, prove
   refusal without a valid audit, then prove a valid audit permits planning.
   Native dispatch needs its own live acceptance evidence.

The current GitHub branch protection and release credential grants were not
queried. Their absence in the October plan is a historical limitation, not a
fresh claim about provider settings. The audit-read service is useful for
derived views but is not a prerequisite for CLI audit execution.

The original acceptance contracts remain in the
[coordination plan](2026-10-15-coordination-layer-plan.md),
[audit tiers plan](2026-10-16-production-audit-tiers-plan.md), and
[audit guide](../audit-workflow.md). Local tests cannot substitute for the
full and release live proofs described above.

## Validation

Both board files passed the exported Dias chip-board parsers and the stricter
180-character text limit. Local validation passed: 385 tests, Ruff lint and
format checks, and source and wheel builds. Four tests were skipped: one
POSIX-specific case and three requiring a configured immutable Docker image.
