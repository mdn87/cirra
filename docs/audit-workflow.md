# Production audits

Cirra can run the lugos `production-audit` skill at three tiers and act on the
results. `pr` audits a change's diff, `release` audits what ships, and `full`
audits the whole repository and becomes the baseline that later audits are
compared against. The skill is the audit procedure; Cirra decides when to run
it, stores and compares results, publishes `pr` findings, and gates releases.
The design and its decisions are in the
[production audit tiers plan](plans/2026-10-16-production-audit-tiers-plan.md).

Nothing changes until the operator policy has an `audit` block.

## Policy

Only `tiers` is required. The other values below are the defaults.

```yaml
audit:
  tiers: [pr, release, full]
  runner:
    kind: session            # or: command, with an argv list (never a shell string)
    command: null
  full:
    schedule_days: 7         # a scheduled full audit once the newest is this old
    max_age_days: 14         # any event queues one once it is this old
  # escalate_paths replaces the default structural list; escalate_paths_add extends it.
  escalate_paths_add: ["scripts/deploy-*.sh"]
  gates:
    pr: critical             # fail the cirra/audit check on new findings at or above
    release: high            # refuse release requests on findings at or above
    full: none               # full audits never block
  limits:
    debounce_seconds: 120
    audit_timeout_seconds: 3600
    max_audits_per_day: 20
```

The default structural paths cover manifests and lockfiles, Dockerfiles and
compose files, `deploy/` and systemd units, migrations, CI workflows, and
Terraform. A pull request touching one of them also queues a full audit of its
head. That audit is scoped to the change and never replaces the baseline.

## When audits run

| Event | Audits |
| --- | --- |
| A pull or merge request is captured by the worker | `pr`, plus a change-scoped `full` when it touches structural paths |
| A release is planned with `plan-release --config` | `release` for the candidate commit |
| The worker starts, then hourly | `full` of the default branch when there is no baseline, the catalog changed, the baseline is `schedule_days` old, or older than `max_age_days` |
| `cirra request-audit` | the named tier |

Requests are queued under `audit-queue/` in the state directory. Per
repository, change or release ref, and tier, at most one request is queued and
one runs. A newer push replaces a queued request after `debounce_seconds` of
quiet and stops a running one at its next checkpoint. A repeated delivery of
the same revision attaches to existing work. Release requests are never
replaced by pull request traffic. Work runs in the order `release`, `pr`,
`full`. When `max_audits_per_day` is reached, `pr` and `release` requests are
shown as `deferred` and retried the next UTC day, and `full` requests wait.

`cirra audit-jobs` lists requests, `cirra cancel-audit --job ID --reason TEXT`
cancels one, and `cirra jobs` and `cirra health` include them on a service
state directory.

## Running an audit

The worker fetches the change or branch into a hardened cache under
`audit-repos/` (no hooks, credential helpers, submodules or redirects) and adds
a worktree for the attempt under `audit-attempts/<attempt>/checkout`. It writes
a packet to `audit-packets/<attempt>.json` with the tier, revisions, the skill
identity Cirra expects, the newest baseline's profile for `pr` audits, and the
catalog domains for `full` audits.

With `kind: session` the job waits as `awaiting_import`. Give the packet to an
agent session that has the skill, then import the report:

```bash
cirra import-audit --attempt ATTEMPT --report production-audit-pr-20261017-120000.json --checkout PATH --state-dir STATE
```

With `kind: command` the worker runs the argv in
`audit-attempts/<attempt>/work` with the reviewer's reduced environment and
`CIRRA_AUDIT_PACKET`, `CIRRA_AUDIT_DIR` and `CIRRA_AUDIT_CHECKOUT` set. The
agent writes exactly one report there, and the worker imports it. For full
audits the agent also writes `checkpoints/<domain>.json` after each catalog
domain and stops between domains when a `stop` file appears. That lets a
`pr` or `release` request preempt a running full audit, which then resumes
without redoing finished domains. A full audit replaced by a newer head reuses
the domains whose cited files did not change.

Outside the worker, `cirra prepare-audit` opens an attempt for a local
checkout and writes its packet, for manual runs.

## Import and comparison

`import-audit` refuses a report for another tier, head or base without using
up the attempt. Otherwise it validates the report with Cirra's vendored copy
of the skill's validator (see `src/cirra/vendor/production_audit/SOURCE.md`),
including the catalog digest and every cited line range at the audited commit.
A report that fails is stored as a failed attempt with the validator output.
A valid report is compared with the newest repository-scoped full audit:
findings are new or open, and a full audit also lists resolved ones. Findings
match by their concept IDs plus path. Importing the same report again is a
no-op.

## Publication and gates

A completed `pr` audit publishes one `cirra/audit` check run on the head (GitHub
only; GitLab is recorded as unsupported). Its conclusion is `failure` when a
finding that is new against the baseline meets `gates.pr`, so a pull request is
not blamed for the backlog. Each audit identity publishes once.

`plan-release --config POLICY` refuses until a release audit of the candidate
commit exists, queueing one if needed, and refuses while it has findings at or
above `gates.release`. `--waive-audit REASON` is the only override. The gate
record goes into the release request and a waiver also writes
`audit-waivers/<request_id>.json`. `dispatch-release` evaluates the gate again
with the recorded waiver.
