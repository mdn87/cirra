# Cirra release completion

This is the executable remainder of Neon's release plan, corrected after the
single Fable cross-review `20261128-094658-cross-review`. Matt authorized
completion on November 28. Preserve completed production and audit evidence;
do not restart historical audits or wait for a soak.

## Required work

| Batch | Outcome | Evidence required |
| --- | --- | --- |
| 1 | Bounded coordinated import with explicit recovery; authenticated audit reads and compatible Taskmaster client; standalone readiness and separate audit warnings; coordinated audit/release commands | Regression tests, independent implementation review, green CI, landed revisions, managed deployment to both hosts and reader/client verification |
| 2 | Backup and restore freshness alerts, Mac-active monitoring, disabled legacy worker, retention inventory and packaging | Effective runtime and alert readback, protected active/rollback/interpreter references, exact cleanup preview, wheel/sdist inspection |
| 3 | One candidate, ordered candidate audits, final metadata and publication | Candidate/merged tree equality, candidate full baseline before release audit import, release gate, fresh recovery copy, checksums and release readback |

Changes to a candidate invalidate evidence for changed content. Rerun only the
affected gates; do not repeatedly audit unrelated historical or documentation
work. Review-count ceilings are advisory. Session audit execution remains the
v1.0 model; a command runner is deferred. The operator required Linux isolation
before publication, and the dedicated account/rootless engine are now deployed.

## Corrections that apply during execution

- Retention already selected: every six-hour capture for 48 hours, then 14 daily,
  8 weekly and 12 monthly recovery points. This is not deletion authorization.
  Scope any future pruning to Cirra; preserve referenced backup/restore points.
- Never reclaim `runtime-3.13`: the Mac deployment launcher still uses it.
  Keep active state, verified recovery copies, rollback releases and referenced
  audit checkouts. Preview exact paths before any removal.
- Linux's effective backup ExecStart already uses `ha-current` through its
  drop-ins. The Mac launcher imports Cirra from `ha-current/src`. Both were
  verified November 28; a base-unit/interpreter path did not prove runtime skew.
- The November 27 production drill is recorded in
  [production rollout](../production-rollout-2026-11-27.md): warm primary role
  withdrawal, 3.45-second Mac takeover, 20 public health samples, signed
  duplicate delivery, majority-loss refusal/recovery and native publication
  recovery. It did not measure physical power loss or Mac cold-boot unlock.
- GitHub does not automatically redeliver failed webhooks. Document the actual
  reconciler and operator delivery-recovery coverage, including missed classes.
- A pre-commit lease check cannot atomically combine SQLite with a quorum
  checkpoint. Report success only after the checkpoint; distinguish a local
  commit whose checkpoint cannot be confirmed and support exact idempotent replay.
- Release dispatch needs a quorum-backed external-effect reservation and
  read-only reconciliation after uncertainty, including after a restored state.
- Audit backlog is a warning, not a receiver/worker availability failure.
  Keep `/healthz` compatible with existing deployment readiness probes.
- Do not add the proposed blanket refusal of multi-day clock advances: it also
  blocks legitimate recovery after downtime. Keep that redesign out of v1.0
  unless a tested reset/time-corroboration contract is supplied.
- The wheel contains the CLI and package data; the source distribution also
  carries tests, operator scripts and examples. HA deployment scripts are a
  site-specific reference installation, not a portable installer.

## Approved decisions and remaining execution

The protected November 28 review approved MIT licensing, GitHub v1.0.0
publication with wheel/sdist/checksums after the release gates, and the
conditional availability boundary. It required Linux isolation instead of
accepting rootful Docker. See [the recorded decisions](../release-acceptance.md).
No further answer to those questions is pending. No grant authorizes PyPI,
permanent data deletion, quota resets or audit waivers.

PR 84 prepared 1.0.0 and deployed a dedicated Linux account with rootless Docker.
Native credentials, resource enforcement, quorum access, authenticated reads,
off-host encrypted recovery and verified Mac caching passed. The rollout also
exposed five brief public 502 responses; the deployment correction drains each
dedicated connector before restarting its origin. Retain this failed observation
alongside the subsequent corrected rollout evidence.

Finish the corrected rollout, build and install-check the exact frozen candidate,
then complete its full baseline and release audit before dispatching release
evidence and publishing. November 28 audit admission is exhausted; preserve the
counter and use the next UTC admission window. Existing historical code-audit
requests retain their explicit dispositions. Destructive cleanup is deferred
outside this release; its preview is not deletion authorization.
