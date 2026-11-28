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
v1.0 model; a command runner and rootless Docker migration are v1.1 work unless
the operator chooses otherwise.

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

## Remaining operator decisions

Prepare concrete evidence before requesting the irreversible steps together:
exact data cleanup and automated retention scope; acceptance of the existing
single-operator Docker privilege boundary; license and distribution choice;
the measured availability promise and exclusions; final tag/release publication.
No current grant approves permanent data deletion or a new license.

Uptime cannot be guaranteed during every possible failure. The proposed v1.0
boundary is warm single-host failover with a verified standby recovery point
less than 24 hours old. FileVault cold boot needs local unlock; loss of quorum
fails closed; WAN/Cloudflare outages can make the service unreachable. Present
these explicitly rather than claiming that all such failures return HTTP 503.
