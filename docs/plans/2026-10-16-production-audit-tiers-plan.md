# Production audit tiers in Cirra

Date: 2026-10-16. Status: stage 1 implemented (`src/cirra/audit.py`); stages 2 to 6
proposed.

## Purpose

The `production-audit` skill (lugos, `plugins/lugos-toolset/skills/production-audit/`,
landed in mdn87/lugos#531 and #533) is a read-only procedure an agent runs
against a repository. It checks the engineering concepts that apply to the
project from a 117-entry catalog and writes a Markdown report plus JSON whose
findings use exactly the fields of Cirra's session-review finding
(`src/cirra/session.py`, `response_schema`).

The skill has three tiers:

| Tier | Scope | Purpose |
| --- | --- | --- |
| `pr` | The diff of one change | Security, correctness, reliability and regressions introduced by the change |
| `full` | The whole repository | The complete architecture-aware assessment and the baseline |
| `release` | What ships and how it runs | Deployment safety, rollback, backups, recovery, observability, secrets |

Today a person picks the tier. This plan makes Cirra pick it from the events
it already handles, run the audit through the same agent hand-off it uses for
reviews, store and compare the results, and apply configured gates. The skill
stays the audit procedure and stays independent of Cirra; Cirra owns
triggering, evidence, baselines and enforcement. That split follows the
governing decision of the
[coordination layer plan](2026-10-15-coordination-layer-plan.md): Cirra
coordinates capabilities instead of rebuilding them.

## Tier selection

Selection is a pure, deterministic function from an event and the stored
audit history to zero or more tiers. No model decides the tier.

| Event | Tiers | Source in Cirra today |
| --- | --- | --- |
| Pull/merge request opened or updated | `pr` | `events.event_hint` and the worker's current-state capture |
| The same, when the change touches structural paths | `pr`, and a queued `full` | Same, plus the escalation patterns below |
| Release request for a protected branch and commit | `release` | `release.plan`, which already binds `expected_sha` and `analysis_digest` |
| Repository added to the policy with no stored full audit | `full` (baseline) | Policy load |
| Schedule tick, stored full audit older than `max_age_days`, or a new catalog version | `full` | New scheduler step in the worker loop |
| Operator command | Any named tier | New CLI command |

Structural paths default to manifests and lockfiles, Dockerfiles and compose
files, deployment and unit files, migration directories, CI workflow
definitions, and infrastructure-as-code. The policy can extend or replace the
list. The rule exists because a change to those files can alter what the
project is, which the `pr` tier, scoped to the diff, cannot assess.

## Policy

A new optional `audit` block in the operator policy, validated by
`config.load_config` with the same strict `fields` checks as the existing
blocks:

```yaml
audit:
  tiers: [pr, release, full]
  runner:
    kind: session            # session (export/import) or command (local agent)
    command: null            # argv for kind=command; never a shell string
  full:
    schedule_days: 7
    max_age_days: 14
  escalate_paths:
    - "pyproject.toml"
    - "**/Dockerfile"
    - "deploy/**"
    - ".github/workflows/**"
  gates:
    pr: critical             # block on new findings at or above this severity
    release: high            # block on unresolved findings at or above this
    full: none               # full audits never block; they reset the baseline
  limits:
    debounce_seconds: 120
    audit_timeout_seconds: 3600
    max_audits_per_day: 20
```

Without an `audit` block, behavior is unchanged.

## Audit identity, storage and baselines

An audit's identity is the digest of: tier, repository identity, head commit,
base commit (`pr` only), skill version, catalog digest, and policy digest. An
identical identity is never run twice; the stored result is reused, the way
`store.attempt` reuses review attempts today.

`cirra import-audit` validates a report with a vendored copy of the skill's
validator (`scripts/validate_report.py --repo <checkout>`, see Decisions)
before storing anything. A report that fails validation is recorded as
a failed attempt with the validator's output, never as a result.

Stored per audit: identity, coverage rows, findings, profile, validator
output, and timestamps. Findings are matched across audits by concept IDs plus
path, so a later audit can mark each finding as new, still open, or resolved.
The `pr` tier publishes only new findings against the latest baseline, so a
pull request is not blamed for the backlog. The newest full audit's profile is
passed into each `pr` packet so the agent skips discovery; this needs one
small skill change (accept a supplied profile).

## Queueing, supersession and cancellation

Local models make audits slow (minutes to an hour for `full`) and a single
GPU host runs one at a time, so repeated events on the same project will
overlap. Cirra already treats webhooks as hints and captures current state at
work time, and publication already refuses reports for an outdated head, so
overlap cannot publish stale results. What it can do is waste the model slot.
The rules below prevent that.

**Work key.** Each audit request has a work key of repository, change (or
release ref, or `repository` for `full`), and tier. At most one request per
work key is queued and at most one is running.

**Coalescing.** When a request arrives and another request with the same work
key is still queued, the newer one replaces it; the older one is marked
`superseded` and never runs. Combined with `debounce_seconds` (a request waits
that long for quiet before it becomes runnable, using the queue's existing
`not_before`), a burst of pushes produces one audit of the last head.

**Supersession of running work.** When a request arrives for a work key whose
audit is running on an older head, the running job is flagged
`superseded_by: <new job id>` and the new job is queued. The runner checks
the flag at its checkpoints and stops. Nothing is lost: the audit had not been
imported, and import would have refused the old head anyway.

**Duplicate requests.** A request whose identity matches a running or
completed audit is attached to that audit instead of creating work. Webhook
redelivery and repeated release requests therefore cost nothing.

**Cancellation is cooperative and fenced.** The runner holds a lease (the
existing `store.lease` and `_fence` mechanism). Cancellation sets the flag;
the runner stops at the next checkpoint, and for `kind: command` the worker
terminates the agent process after a grace period. Any late import from a
cancelled attempt fails the fence check. A cancelled job is final, never
retried.

**Checkpoints and partial reuse.** A `full` audit runs domain by domain (the
skill already allows splitting step 3 by domain). Each completed domain's
coverage rows are stored under the identity plus the domain. A superseded or
preempted full audit resumes from the completed domains when the head is
unchanged, and reruns only the domains whose files changed when it is not.

**Priority and preemption.** Runnable work is ordered `release`, then `pr`,
then `full`, then by acceptance time. A `pr` or `release` request may preempt
a running `full` audit at a domain checkpoint; the full audit is requeued and
resumes from its checkpoints. `pr` and `release` audits are never preempted.

**Release isolation.** Release requests are keyed by release ref and expected
commit. Pull request traffic never supersedes them. A new release request for
a different commit on the same ref supersedes the older one.

**Admission.** `max_audits_per_day` mirrors the worker's existing daily
admission limit. When the limit is reached, `full` work waits for the next
day, while `pr` and `release` requests are recorded as `deferred` and shown in
status, so a gate is never silently skipped.

## Gates

- **`pr`**: findings at or above `gates.pr` that are new against the baseline
  are published through the existing advisory publication path. The native
  check conclusion follows the gate; native merge rules stay with the provider.
- **`release`**: `release.plan` refuses unless a stored `release` audit exists
  for `expected_sha` with no unresolved findings at or above `gates.release`.
  An operator waiver, recorded with a reason, is the only override.
- **`full`**: never blocks. It replaces the baseline and reports what was
  resolved and what is new.

An audit that times out, fails validation, or is deferred leaves the gate
`pending`, never passing.

## Stages

Each stage lands on its own with tests and leaves Cirra working.

### Stage 1: policy and tier selection

The `audit` policy block with validation, and the pure selection function
(event plus history to tiers, including structural escalation and baseline
age). Acceptance: table-driven tests cover every row of the tier table;
policies without an `audit` block load unchanged.

Implemented as `cirra.audit.load_audit_policy` (called by `load_config`) and
`cirra.audit.select_tiers`, tested in `tests/test_audit_tiers.py`. Choices made
while implementing:

- Only `tiers` is required. Omitted sections take the values in the policy
  example above, with `runner.kind: session`.
- `escalate_paths` replaces the default structural list;
  `escalate_paths_add` extends it. A policy may use one, not both. In patterns,
  `**` spans directories and `*` stays within one.
- A missing baseline, a changed catalog digest, and a full audit older than
  `max_age_days` are checked on every automatic event, as the table lists them
  separately from the schedule tick. The tick itself selects `full` once the
  newest full audit is `schedule_days` old, so `max_age_days` must be at least
  `schedule_days`. At most one `full` is selected per event, with the first
  matching reason: `baseline`, `catalog`, `stale`, `structural`, `schedule`.
- An operator command for a tier not listed in `tiers` is refused.
- Nothing calls `select_tiers` yet; the worker wiring is stages 3, 4 and 6.

### Stage 2: audit import and storage

`cirra prepare-audit` (export a packet with tier, base, head, profile and
catalog version) and `cirra import-audit` (validate with the skill's
validator, then store coverage, findings and identity). Identity reuse and
baseline matching. Acceptance: importing the same report twice is a no-op;
an invalid report is stored as a failed attempt with the validator output; a
second audit marks findings new, open or resolved.

Implemented in `cirra.audits` and `cirra.audit_cli`, tested in
`tests/test_audit_import.py`. The validator and catalog are vendored under
`src/cirra/vendor/production_audit/` (see its `SOURCE.md`). The lugos-side
identity change had already landed (skill 1.1.0 records `skill_version` and
`catalog_digest`). Choices made while implementing:

- Attempts live in a new `audits` table (state schema version 2). An identity
  with a prepared or complete attempt is reused; after a failed, cancelled or
  superseded attempt, preparing again opens the next attempt number.
- The identity's skill version is the vendored one. The agent's own
  `skill_version` is stored with its report; only the catalog digest must match.
- A report for another tier, head or base is refused without spending the
  attempt. Validator problems, including a catalog mismatch, fail it.
- The attempt's state is the cancellation fence: import re-checks it in the
  transaction that stores the result.
- Findings match by concept IDs plus path. Only a full audit can mark a finding
  resolved, since `pr` and `release` audits cover a subset of concepts.
- The newest full audit's profile goes into `pr` packets. Whether the skill uses
  it is still the skill's change to make.

### Stage 3: queueing rules

Work keys, coalescing, debounce, supersession flags, cooperative fenced
cancellation, priority ordering, and daily admission for audits, in
`job_queue.py` and the worker loop. Acceptance: tests for a burst of three
pushes (one audit, of the last head), a push during a running audit (running
job superseded, late import refused), a duplicate delivery (attached, no new
work), and a release request during pull request traffic (never superseded).

Implemented as `cirra.job_queue.AuditQueue` (file records under
`audit-queue/`, separate from review jobs) and `cirra.audit_worker.audit_once`,
with `cirra request-audit`, `cancel-audit` and `audit-jobs`; tested in
`tests/test_audit_queue.py`. Choices made while implementing:

- Superseding or cancelling running work also marks its stored attempt
  `superseded` or `cancelled` at once. That state is the fence: import refuses
  any attempt that is not `prepared`, so no lease is held across an agent run.
- A session runner leaves its job `awaiting_import`, which does not occupy the
  worker; each pass settles such jobs once the operator imports the report.
- A request whose identity is queued, running, awaiting import, done, or
  complete in the store attaches to that work.
- Deferred `pr` and `release` requests become runnable again on the next UTC
  day; `full` requests stay queued. Admission counts in `audit-usage/`.
- A runner error fails the job and fences its attempt.
- Preemption (stage 6) and the worker wiring (stage 4) build on this pass.

### Stage 4: `pr` tier in the worker

Webhook-triggered `pr` audits through the same path as reviews, with
`kind: session` first and `kind: command` for a local agent. Publication of
new findings and the check conclusion. Acceptance: a live pull request on
the Cirra repository produces one audit, one publication, and no
republication on redelivery.

Implemented in `cirra.audit_worker` (`request_audits`, `AuditRunner`,
`AuditPublication`), `cirra.audit_checkout`, and the worker loop; tested in
`tests/test_audit_worker.py` against a local origin with a real
`refs/pull/N/head`, a stand-in agent process, and a fake GitHub API. The live
acceptance on lugos-host is still open: it needs a release deployed with an
`audit` block in the route policy. Choices made while implementing:

- The review processor queues audits right after it captures a change, before
  observation and review. Audit errors are recorded on the review job and never
  fail it; the worker loop runs one audit pass after each review pass.
- The worker fetches the change ref into a hardened bare cache per repository
  (no hooks, credential helpers, submodules or redirects; HTTPS only) and adds a
  detached worktree per attempt. Worktrees of final attempts are removed.
- `kind: session` exports the packet and leaves the job awaiting import.
  `kind: command` runs the argv in the attempt's work directory with the same
  reduced environment as the Claude reviewer plus `CIRRA_AUDIT_PACKET`,
  `CIRRA_AUDIT_DIR` and `CIRRA_AUDIT_CHECKOUT`. The agent writes one report
  there; the worker imports it. Stopping writes `stop` and, after a grace
  period, terminates the process tree.
- Baseline, catalog and age reasons request a full audit of the default branch
  head (read from the provider) with scope `repository`. A structural full
  audit runs on the change head with scope `change:N` and never becomes the
  baseline (state schema 3 adds the scope column).
- Only `pr` audits publish: one `cirra/audit` check run per identity, found by
  external ID before any write, with a receipt under `audit-publication/`. The
  conclusion is `failure` when a new finding meets `gates.pr`, `success`
  otherwise, `neutral` for `none`. Publication failures put the job in
  `attention`, which `cirra health` reports. GitLab publication is not
  implemented and is recorded as `unsupported`.

### Stage 5: release gate

The `release` tier on release requests and the gate in `release.plan`, with
waivers. Acceptance: `plan` refuses without a current audit and with a
blocking finding, and proceeds with a waiver whose reason is recorded.

### Stage 6: full audits, schedule and preemption

Baseline on first registration, the schedule and age triggers, structural
escalation from stage 1 wired to the queue, domain checkpoints, resume, and
preemption by `pr` and `release` work. Acceptance: a preempted full audit
resumes without rerunning completed domains; a catalog version change
triggers a new baseline.

## Decisions

Recorded 2026-10-16 by the operator. Each replaces an open question from the
first draft.

**The validator is vendored into Cirra.** Cirra runs as a service and must not
depend on a lugos checkout being present or current. Stage 2 copies
`validate_report.py` and the catalog it reads into Cirra with a note of the
lugos commit they came from. The report-to-Cirra contract is the JSON format,
so Cirra owns its own check of it. To make drift detectable, stage 2 also
needs one change on the lugos side: reports record the skill version and the
catalog digest, and `import-audit` refuses a report whose catalog digest
differs from the vendored one, with a message naming both, instead of
validating it against the wrong catalog. Updating the vendored copy is a
deliberate Cirra commit.

**Checkpoints are plain files.** For `kind: command`, the worker gives the
agent an attempt directory. The agent writes one JSON file per finished
domain there, named by domain, each holding that domain's coverage rows, and
writes the final report last. The worker reads those files to track progress
and to resume. To cancel, the worker writes a `stop` file that the agent
checks between domains, then terminates the process after the grace period
if it is still running. Files survive a worker restart and need no protocol
beyond the directory layout, so any agent can implement them.

**`pr` audits and the AI review run separately.** Each keeps its own agent
run, output format, failure handling and evaluation. Stage 4 records how
often the two report the same issue on the same lines; merging them into one
run is reconsidered only if that overlap is high enough to justify losing
the separate attribution.
