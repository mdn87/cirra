# November 28 release completion evidence

This records the operational work for the planned v1.0.0 release. It is not a
claim that the final version, candidate audits or GitHub Release are complete.

## Landed and verified

PR [77](https://github.com/mdn87/cirra/pull/77) added authenticated audit reads,
bounded coordinated import recovery, fenced operator/release commands, separate
audit warnings and independent health publishing. PR
[78](https://github.com/mdn87/cirra/pull/78) corrected the real systemd credential
format: root-owned 0440 ACL files in the service's private credential mount.
Both passed all seven checks. PR 78's Linux Python 3.11 suite passed 792 tests
with eight optional skips; Linux/Windows Python 3.11/3.14 and dependency checks
were green. The built wheel imported and completed the offline review fixture;
the source archive contained the operator scripts and service examples.

Both data hosts deployed `64fc34391831c9bb408f8e683dbcf00c6a3a3f95` through the
managed release selector. Linux resumed as the active worker and the Intel Mac
as standby. Their verified deployment journals retain the previous release
`4a74d3e570945a9f4ea0ac1208ef206d6d536514` and exact configuration for rollback.
No schema change, reenrollment or budget reset occurred. This records verified
rollback preparation, not a new live rollback drill.

Taskmaster [PR 25](https://github.com/mdn87/taskmaster/pull/25), revision
`e87ac5c5bafffdbc3fe770b28b82ba32517aa22a`, is deployed. Its real authenticated
client read one existing audit and gate. The `autowork` and `uosserver` accounts
received HTTP 401 and could not read either credential file. The public reader
health route remained HTTP 200. Taskmaster's existing saved-view configuration
was preserved; this verifies the live client adapter separately.

The new coordinated operator command cancelled the obsolete documentation-only
PR 76 audit. Its native receipt reported `state: complete` and
`authority_checkpointed: true`. Existing code audit requests retain their
actual state and daily admission accounting.

## Monitoring and recovery

Nodens Uptime Kuma has four active Cirra monitors: public receiver, active
worker, Linux recovery health and Mac recovery health. All use the configured
Pushover channel. Linux's real scheduled service sent both host and active
heartbeats; the healthy Mac standby sent only its host heartbeat. Backup and
donor-refresh failures also reach the Linux health alert unit.

A controlled Mac monitor DOWN at 11:14:53.400 UTC was followed by an UP from
the real reporter at 11:14:54.122 UTC. Native logs contained no notification
send errors in the observation window. The separate native notification test
received provider success. Kuma stores no per-cycle provider receipt, and
human receipt of those alerts was not observed.

Twenty post-rollout public health requests returned HTTP 200. An earlier probe
using Python's default user agent received Cloudflare error 1010/HTTP 403;
identified clients succeeded. Those rejected probes cannot establish continuous
availability during the rollout. The separate November 27 warm takeover proof
remains in [the earlier rollout record](production-rollout-2026-11-27.md).

A new private capture at 11:14:02.429805 UTC was encrypted, independently restored
and verified, and fetched/verified by the Mac's native donor refresh. Its capture
identity is `7c00321a6c2ce980d1d09d9859927f1e3014c2e22e75a7aa542ce48f40d8b927`;
the Restic snapshot is
`0c2143fa26fe23bf0a624111a439a2e77ef8090e3c1bfb8dcd03a58836d24d5f`.
The Mac's 527,354,298-byte cached archive matched its recorded SHA-256. Normal
refresh keeps this verified cache separate from the currently installed state;
the worker installs it when recovery requires it.

The live check exposed a health discrepancy: an installed restore could age
even while the promotion archive was refreshed. The accompanying correction
checks the private native receipt and hashes the configured cache within size
and time limits. Its preflight verified the real Mac cache in 1.35 seconds;
109 focused tests passed, with three platform-specific skips. This preflight
used the proposed code separately from the installed runtime; managed rollout
and its final service readback remain separate evidence.

## Retired components and retained data

The old standalone worker and webhook units are disabled. The unused snapshot
scripts and unloaded Mac plist were archived in private dated retirement
directories. The exact legacy forced-export key entry was commented after a
recoverable backup. The operator SSH entries remain usable.

No private state or ZIP was deleted. Old Linux state and a Mac restore still
have incoming Git worktree references. The selected six-hour captures for
48 hours, 14 daily, eight weekly and 12 monthly retention policy removed zero
of the 25 snapshots present at the preview. Five verified code releases per
host were protected by the active/rollback/three-extra rule; older directories
had insufficient immutable installation receipts for the bounded cleanup.
That preview is not authorization for later pruning.

## Remaining release gates

The prepared MIT license, metadata patch and release notes require the
operator's licensing/publication choice. The existing rootful Docker boundary
and conditional single-site availability promise also require explicit risk
acceptance. Destructive cleanup is deferred; runtime and private recovery data
remain protected.

After those decisions, freeze one versioned candidate, complete its repository
full baseline before its release audit, verify the merged tree and built assets,
then reconcile Cirra's native evidence workflow before publishing the approved
tag and GitHub Release. The current UTC day's audit ledger is 10/10; it was not
reset or bypassed. The v1.0.0 gate is not satisfied by queue admission or by
this operational record.
