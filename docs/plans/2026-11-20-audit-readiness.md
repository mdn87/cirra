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

## GitLab reader recovery

The initial health check reported a 401. To distinguish an expired token from
a launch or repository-access problem, the recovery check followed the worker's
configured Glas reference through to a GitLab read. Recovery required the
enabled GitLab route to reconcile, the application health check to pass, and
the alert service to resolve the outstanding alert.

### Recovery check at 06:49 UTC

GitLab identified the deployed reader as expired. Bitwarden contained two
entries with the same name in the same project. The November entry could read
the GitLab project, open merge requests, the main branch, a pipeline and its
job list. A shallow Git fetch matched the branch revision. The raw log for job
`17222748463` returned HTTP 403 with `insufficient_granular_scope`. Reading
that endpoint requires **Job → Read**. [GitLab job permissions](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens_rest/#job)

The worker's Bitwarden machine account returned 404 for the November entry
and could read the original October entry. After restoring both launchers to
the October reference, the worker restarted and wrote a fresh heartbeat.
GitHub reconciliation succeeded. GitLab reconciliation still returned 401
and the health alert remained open.

### Recovery verified at 07:01 UTC

After the operator updated the original Bitwarden entry, the worker's machine
account read the log for the same GitLab job, `17222748463`. GitLab returned
HTTP 200 and the check read 512 bytes of plain text. The job appeared in
pipeline `2953821104`, and a shallow fetch of `main` matched the revision
reported by the branch API.

The worker restarted at 07:00:02 UTC and reconciled GitLab and GitHub at
07:00:07 UTC. The health check exited zero with no problems. At 07:01:05 UTC
the alert service accepted the resolution with HTTP 202 and the pending alert
marker cleared. GitHub PR audits 24, 25 and 28 still awaited import.

## Three PR audits completed at 07:42 UTC

Each report passed the Lugos validator and the deployed Cirra validator against
the base and head in its original packet. Cirra stored all three reports.
The worker marked each job done. GitHub returned one completed `cirra/audit`
check on each head, with the same audit ID and conclusion as the worker receipt.

| Pull request | Audited head | Findings | Published check |
| --- | --- | --- | --- |
| #24, webhook redelivery documentation | `65bf815` | None | [Passed](https://github.com/mdn87/cirra/runs/136155387332) |
| #25, health alert recovery | `ea4f0f5` | Three medium | [Passed](https://github.com/mdn87/cirra/runs/136155397022) |
| #28, loopback audit read API | `ff88dad` | Four medium, one low | [Passed](https://github.com/mdn87/cirra/runs/136155405276) |

All eight findings fell below the configured critical threshold.

- **Alert delivery.** The alert unit can report success after skipping delivery
  when its credential is absent. Its journal read has no deadline, and the
  automated suite does not exercise the alert and recovery notification cycle.
  The configured recovery path did deliver the November 20 resolution.
- **Audit read API.** The example service grants write access to Cirra state.
  A concurrent receipt update can make the gate record and its generation
  disagree. Each request scans the entire audit history and all receipt files,
  and listing summaries loads full reports into memory. Invalid numeric page
  parameters can also produce an HTTP 503 response. The service was reviewed
  from its pinned revision and was absent on the production host.

The live queue had zero jobs waiting for import, and the health check reported
zero problems. Each stored comparison had an empty baseline reference. The
store contained no full or release audit. The operator copies of both report
formats are under `/srv/lugos/state/cirra/operator-audit-reports/<attempt_id>/`.

## Full baseline and PR 33

The GitHub route now enables `pr` and `full` with the session runner. The PR
gate remains `critical`, and the full gate is `none`. The previous policy is
saved on the host for rollback. Both services reloaded the policy, and the
worker automatically requested the first repository baseline on `883d840`.

| Audit | Attempt | Result |
| --- | --- | --- |
| Repository full, `883d840` | `15ebe3768a174989a832895ac6d4dd40` | All 117 concepts assessed, 32 findings. |
| PR 33, `de96adf` | `98fe8e93ce61466db649bc9558c6be07` | Same repository baseline, zero new findings, two existing findings below the gate. |
| Change-scoped full, `de96adf` | `7e656ff4ca0e406c910bc4ba4049b5cb` | Same repository baseline, 31 open findings and one resolved. |

The resolved finding concerned a health alert that returned success when its
credential was missing. The regression tests failed on the old command and
passed on the fix. The two open PR findings concern the journal read deadline
and the transport used for a configurable notification recipient. Three
findings had high priority. Promotion can accept a failed worker, rollback
across a schema change can strand the older binary, and recovery of signed
state from independent storage remains unverified.

Both validators accepted all three reports against their pinned revisions.
The worker finished all three jobs, and the selected repository baseline still
points to the full audit of `883d840`. The PR merged at `ca66dbb` after the
host's isolated tests, the baseline-backed audit and all five GitHub Actions
jobs passed. [PR 33](https://github.com/mdn87/cirra/pull/33) ·
[Isolated tests](https://github.com/mdn87/cirra/runs/136168360421) ·
[PR audit](https://github.com/mdn87/cirra/runs/136169212102) ·
[CI matrix](https://github.com/mdn87/cirra/actions/runs/44700579454)

The separate advisory review returned an uncertain result. Claude Code on
`lugos-host` reported that the subscription was signed out. The worker retained
the job for operator attention and GitHub received no `cirra/review` on this
commit. Both services were active. The health check reported that one job,
and zero audits were waiting for import. The merged alert template requires
a separate unit rollout on the host.

## Remaining operational proof

1. Restore the worker's Claude subscription login. Use a real PR to verify
   that the host publishes both its isolated test result and a completed
   advisory review on the same commit.
2. Prepare the separate release demonstration with a protected target, a merged
   candidate with witness-verified CI definition evidence, a trusted native
   workflow and the restricted Actions credential. Enable `release`, prove
   refusal without a valid audit, then prove a valid audit permits planning.
   Native dispatch needs its own live acceptance evidence.

GitHub reported `main` unprotected with no active branch rules at merge time.
Release credential grants still need verification. The audit-read service
provides derived views. CLI audits operate directly against the store.

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

## Remediation and release checks

The protected main branch now requires the host's isolated tests and code review
on the same pull request head. PR 38 exercised the deployed worker, produced a
signed build receipt and received a completed Claude review. PR 40 also passed
both host checks and the four GitHub test jobs using hash-locked dependencies.
[PR 38](https://github.com/mdn87/cirra/pull/38) ·
[PR 40](https://github.com/mdn87/cirra/pull/40)

The release planner refused the PR 38 candidate twice. The first attempt lacked
a release audit. A later 45-control release audit found three high-severity
issues, and the planner again exited without writing a request.

The deployment of the locked observer passed 422 host tests. The first rollout
restored the previous release because the credential launcher starts the worker
as a child process. A Linux regression reproduced the failure. The retry verified
the child's ancestry and systemd invocation and the service passed its health
check. An isolated restore from the encrypted off-host backup passed SQLite
integrity and foreign-key checks and verified all 321 signed receipts.

PR 42 passed both required host checks and its audit recorded two findings. A
paused retention manifest held a SQLite read lock, and a conflicting webhook
delivery counted against service availability. Regression tests reproduced
both findings. The release candidate closes the database between manifest pages
and excludes conflicting delivery IDs from the admission indicator.
[PR 42](https://github.com/mdn87/cirra/pull/42)

## November 26 audit settlement and operating measurements

The worker completed the eight historical audit requests for PRs 43, 46, 47,
48, 49, 51 and 52. The operator matched each of the seven GitHub check IDs,
head commits, conclusions and summary digests to its publication receipt.
PR 43 also received a 117-control full audit scoped to that change. The
repository baseline remained at `253ab19e7869`.

| Measurement | What the records show |
| --- | --- |
| Host CPU and memory | Across November 19–25 UTC, 1,007 roughly ten-minute sysstat samples covered 167.8 hours on six CPUs. The lowest interval-average idle CPU was 44.13%. Available memory stayed at or above 16.38 GiB of 31.09 GiB. These host-wide intervals can hide brief spikes. |
| Cirra service memory | At inspection on November 26, the receiver had peaked at 29.5 MiB against 256 MiB. The worker had peaked at 93.3 MiB against 1.5 GiB. These cgroup peaks cover the service activations that began at 03:39 UTC. |
| Request latency | The retained logs for the window contain four webhook POSTs, all on November 25. All returned 202 and took 63.85–186.48 ms in the handler. Journal retention was incomplete. Four observations cannot establish a representative p99. The optional audit reader was inactive and supplied no data-request timings. |
| Backup recovery | The November 25 capture paused writers for 3.74 seconds. The restore and verification took 9.17 seconds, passed SQLite integrity and foreign-key checks, and verified 168 current and 247 historical witnesses. This measures state restoration. A whole-host rebuild also needs provisioning, credential recovery and service startup. |

On November 26 the recovery helper restored the November 25 snapshot using
the BWS witness socket. It verified all 415 current and historical witnesses,
passed both SQLite checks and finished in 9.25 seconds. The webhook and worker
kept the same systemd invocation IDs throughout the drill. Restoring an older
snapshot verifies recovery but does not advance the latest capture time.

Matt set a maximum data-loss window of 24 hours and requires uninterrupted
service after host loss. The installed receiver, state store and worker run
on one machine. Meeting that availability target requires an independent
ingress path, durable admission and a way to prevent both workers from
publishing the same review during failover.

The public webhook uses Tailscale Funnel. Tailscale documents relay routing
and a fixed bandwidth limit, without publishing a numeric limit or a
Funnel-specific denial-of-service commitment there. Cirra's local admission
limits protect the receiver. Upstream capacity remains unverified.
[Tailscale Funnel limitations](https://tailscale.com/docs/features/tailscale-funnel#requirements-and-limitations)

## November 26 reader and recovery measurements

At 18:27 UTC the live, unprivileged audit reader served 45 attempts across
35 heads and 28 gate receipts to the Taskmaster client. It used schema 3.
The retained seven-day sample ending at 19:06 UTC contained 129 reader GETs.
The median took 1.00 ms and p95 took 4.63 ms. Seventeen returned 404 for
heads without a gate receipt. All 25 webhook POSTs in the sample returned
202. Their median took 73.84 ms and the slowest took 544.94 ms.

Both data hosts verified the 18:30 UTC recovery point. SQLite integrity and
foreign-key checks passed, and the signed evidence included 220 current and
299 historical witnesses. In a second rehearsal at 19:21 UTC, the Intel Mac
ran the two service origins against three independent voters on that machine. All 45 health
probes returned 200 during takeover and the standby completed the recovered
test job in 6.25 seconds. A physical host loss behind the public webhook still
requires the three-host rollout and an end-to-end drill.
[Service rehearsal](../coordinated-service.md#repeat-the-service-rehearsal)
