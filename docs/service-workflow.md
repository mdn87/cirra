# Automatic PR reviews and independently observed tests

An open, non-draft pull request can trigger the complete flow: an authenticated
webhook enters a durable local queue; the worker fetches the current change,
runs the selected tests in an isolated Docker container, obtains an AI review,
and publishes the results. GitHub still owns the repository, checks, branch
rules, and merge decision. Cirra never merges a PR.

The worker also supports GitLab merge-request reviews, isolated tests, and
maintained advisory notes. GitHub observations attest the supervisor's native
check. GitLab observations use a signed `observer_run` record retained by Cirra;
the maintained MR note reports its result without creating a GitLab pipeline.
Existing GitHub Actions and GitLab pipeline observations retain their original
provenance; this observer does not certify those other runners.

## Configure a trusted installation

Keep configuration, credentials, and service state outside the tested checkout.
The examples are templates: copy them into an operator-controlled directory,
then select the real repository identity, credential references, and paths.
Relative paths resolve against the service configuration file.

1. Install Cirra with its `publish` extra in a dedicated virtual environment.
   The worker also needs a logged-in Claude Code CLI and a local Linux Docker
   engine. Windows with Docker Desktop's Linux engine is supported.
2. Build `examples/observer.Dockerfile` from trusted inputs:

   ```text
   docker build -f examples/observer.Dockerfile -t cirra-observer:local examples
   docker image inspect --format {{.Id}} cirra-observer:local
   ```

   Put the resulting immutable `sha256:...` image ID in a copy of
   `examples/observer.yaml`. Select `docker_context: default` on a typical Linux
   server, or `desktop-linux` for Windows Docker Desktop. Network access is
   available during this operator-controlled image build. PR containers cannot
   install missing dependencies from the network; rebuild the trusted image
   when dependencies need to change.
3. Run `cirra observer-definition --config <observer.yaml>`. Put its
   `definition_digest` in the review policy's expected `cirra/isolated-tests`
   check, together with the exact publisher App ID for GitHub or the
   `observer_run` kind and `cirra/docker` source for GitLab. The digest binds the image,
   commands, limits, launcher, and supervisor implementation. Recompute it after
   an observer upgrade or a move to another installation.
4. Configure `examples/service.yaml` with that review policy and observer file.
   The GitHub App key is a file reference. The worker mints repository-scoped
   tokens with Checks write and Metadata read. Private repository collection
   needs a separate reader token that can also fetch the Git repository.
5. Generate independent, high-entropy webhook and witness secrets of at least
   32 characters. Store them in protected environment files, never in the repo.
   A receiver file needs `CIRRA_GITHUB_WEBHOOK_SECRET`; a worker file needs
   `CIRRA_WITNESS_KEY` and any explicitly configured reader/publisher tokens.

The service reads an environment file only with `--env-file`. It selects only
the credential names needed by that process, does not expand shell expressions,
and lets explicitly set environment variables take precedence. It never loads
`.env` implicitly. The model and sandbox do not inherit these credentials.

## Start the receiver and worker

Run these as separate processes with the same state directory:

```text
cirra serve-webhooks --service-config <service.yaml> --env-file <receiver.env> --state-dir <state> --port 8796
cirra worker --service-config <service.yaml> --env-file <worker.env> --state-dir <state>
cirra jobs --state-dir <state>
```

`worker --once` handles one available job and exits. A normal worker stays
running. `examples/systemd/` contains restartable Linux service templates.
The selected account must have Docker access. Claude review uses that account's
own login; never copy another account's session credentials. The worker starts
without a Claude login so independent CI can still run. An unavailable reviewer
leaves the review needing attention while retaining the separate CI result.

### Backup CI without GitHub Actions

The observer runs directly on the configured host; Actions does not schedule or
execute it. The example policy runs lint, formatting, tests and a distribution
build with preinstalled dependencies (`python -m build --no-isolation`).
This is Linux/Python 3.11 coverage, not the Windows/Python 3.14 matrix in the
repository's Actions workflow. It tests the PR head, not a synthetic merge.

Keep `review_enabled: true` (the default) on a route for the existing Claude bug
review, or set `review_enabled: false` for CI only. CI-only routes must configure
an observer. A failed test remains a failed test even when its job is `done`:
that state means processing finished. `worker --once` exits nonzero for failed
CI or a job requiring retry/attention.

Enable missed-event recovery in the operator service configuration:

```yaml
worker:
  reconcile_seconds: 900
  retry_seconds: 30
  max_retry_seconds: 3600
  max_attempts: 10
  max_jobs_per_day: 30
```

Every 15 minutes the worker lists open non-draft PRs/MRs and reads their current
target branches. Each head/target/policy combination queues once through this
path. Webhook and polling jobs can both exist, but completed tests and model
responses are reused. Failures in one provider's scan do not prevent another
route or queued work from being processed. Polling errors appear in `cirra jobs`.
Set `branches: [main]` on a route to also queue each new main-branch commit.
Branch CI reads and verifies the current branch tip, uses the same isolated
tests and native check, and skips AI review. Branch runs are discovered by
polling or requested manually; the existing webhook remains PR/MR-only.
Provider reads and Git downloads must still be available; this is an Actions
backup, not an offline replacement for GitHub hosting.

Operators can request CI without fabricating a webhook:

```text
cirra enqueue --service-config <service.yaml> --state-dir <state> --route cirra-github --change <PR-number>
cirra enqueue --service-config <service.yaml> --state-dir <state> --route cirra-github --branch main
cirra sync --service-config <service.yaml> --env-file <worker.env> --state-dir <state>
cirra requeue --service-config <service.yaml> --state-dir <state> --route cirra-github --job <job-id> --reason "provider recovered"
cirra jobs --state-dir <state>
```

`enqueue` requests fresh provider state; it reuses an existing test execution and
review for unchanged inputs. `requeue` only accepts terminal jobs and archives the
prior job and operator reason before resetting its attempts. It does not reset
the daily budget or force an uncertain model call/native write to repeat.
Inspect those separately using the existing review/publication recovery flow.

Each finished execution has a signed standalone receipt before GitHub publication
is attempted. The worker also records the latest CI conclusion, tested SHA,
receipt path and publication status under `ci/`, displayed by `cirra jobs`.
If native publication is unavailable, retry resumes from the saved execution.
The independent receipt does not authenticate other providers' CI jobs.
Worker status includes the last loop heartbeat; it can remain unchanged during
a bounded test/review execution and is not a readiness guarantee.

The HTTP listener binds only to `127.0.0.1`. Put an existing HTTPS reverse proxy
or tunnel in front of it. For an installation using Tailscale Funnel, a dedicated
HTTPS port can forward to this listener without changing other served ports:

```text
tailscale funnel --bg --https=8443 http://127.0.0.1:8796
```

Use the resulting public URL plus `/hooks/cirra-github` for the example route.
Configure a GitHub repository webhook with content type `application/json`,
SSL verification enabled, the same webhook secret, and **Pull requests** events.
A repository webhook works alongside the Checks-publishing GitHub App; the App
does not need to receive the delivery itself. GitLab routes use
`/hooks/<route-name>`, Merge request events, and their configured secret token.

Only changes from authors with write access are queued. On GitHub that means a
pull request whose `author_association` is `OWNER`, `MEMBER`, or
`COLLABORATOR`; on GitLab, a merge request whose source branch is in the target
project. Anything else, including fork pull requests from outside accounts and
bots without write access, is acknowledged and ignored with the reason
`untrusted author`, so it spends no execution, review, or daily admission.
Title, description, and label edits that change neither the head nor the base
are ignored as `metadata-only edit`. Run such a change by hand with
`cirra review` after reading it.

The receiver acknowledges after an atomic queue write, without waiting for a
provider, test run, or model. It limits request bytes, connections, and time;
rejects duplicate/framing headers; and accepts only configured repository
identities and supported PR/MR actions. GitHub ping deliveries are acknowledged.
`GET /healthz` checks the receiver, not worker/model readiness.

## What the observer verifies

The trusted supervisor fetches the PR/MR head ref from the configured target
repository, verifies its full commit ID, and reads its Git objects directly.
GitHub uses `refs/pull/<number>/head`; GitLab uses
`refs/merge-requests/<number>/head`. Credentials are restricted to that target
repository's HTTPS URL; redirects and fork-supplied clone URLs are not used.
It never checks out or executes PR code on the host. Hooks, filters, replacement
objects, submodules, and attribute-based export rewriting are not used.
Exports are limited to 64 MiB and 5,000 regular files; symlinks and submodules
are refused. The Git fetch has a deadline; the export limit is not a Git pack
download quota.

The supervisor mounts only a source archive, command list, and trusted launcher
into a new container. The container runs as UID 65534, with no network, a
read-only root and input mount, no capabilities, no Docker socket, and bounded
memory, CPU, process count, logs, and runtime. It gets temporary writable work
and scratch space. The image must already exist by exact ID in a local Docker
daemon and must not declare persistent volumes. This uses Docker isolation;
the trusted host, Docker daemon/kernel, and approved image remain in the trust
boundary. No fleet scheduler or repository workflow interpreter is introduced.

Only the supervisor signs the statement. It observes the actual Docker result
and binds the verified source commit, parents, and execution definition to its
own `cirra/isolated-tests` result, including identity, start time, and
conclusion. GitHub's version 1 receipt attests the current native check. GitLab's
version 2 receipt carries its own standalone check and binds the entire captured
MR snapshot, including repository, MR number, head, target and diff base. Only a
valid signature from the selected witness key can introduce this separate CI
evidence; it never authenticates a GitLab pipeline job. A failing test produces
a verified failure. Uncertain execution or
publication cannot produce a successful witness. PR code can change its own
tests; a verified checkout is not an assessment of test quality.

The worker re-reads the provider after observation and before publication. A
moving head/target causes a refresh and retry. Closed/draft changes are skipped.
The `cirra/review` result remains advisory even when all evidence is complete.

## GitLab setup

`examples/gitlab-service.yaml` and `examples/gitlab-worker-policy.yaml` show the
corresponding route. It can be added to the same service configuration as GitHub;
both routes share the durable worker. Select sandbox-appropriate test commands
in a separate observer policy and pin its computed definition digest.

The private sandbox reader needs API reads for project/MR/diff/pipeline/job
collection plus **Code → Download** for Git-over-HTTPS. The publisher remains a
separate project-scoped token with Work Item Create/Read/Update and an explicit
operator permission receipt. Neither the observer nor the publisher needs
repository-write or merge permission. The webhook uses Merge request events,
TLS verification and `CIRRA_GITLAB_WEBHOOK_SECRET` at `/hooks/cirra-gitlab`.

To change reader permissions, create a replacement fine-grained token with the
existing reads and Code Download, then replace `GITLAB_TOKEN` in the local and
worker credential files. Keep `GITLAB_PUBLISHER_TOKEN` unchanged. GitLab's
[Rotate operation](https://docs.gitlab.com/user/profile/personal_access_tokens/#rotate-a-personal-access-token)
preserves the original permissions. Verify the replacement before revoking the
old token. Restart the worker to load the replacement; a delivery that exhausted
its retries also needs explicit operator recovery with its prior record retained.

Native pipeline-dependent merge rules stay authoritative. Requiring the local
observer in Cirra policy does not replace those rules or attest existing native
jobs. Source fetching, Docker isolation, retry limits and review recovery are
the same as the GitHub route.

GitLab documents the [MR head ref](https://docs.gitlab.com/user/project/merge_requests/merge_request_troubleshooting/)
and the [Code Download permission for Git operations](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens_other/).

## Recovery and operating limits

Queue and observer receipts use atomic files; review state lives in SQLite.
Schema changes are applied at startup through ordered migrations, so existing
reviews and attempts carry over; a database written by a newer Cirra is refused
rather than downgraded. Run one worker and one
receiver against a local filesystem, not a network share or synchronized folder.

Duplicate authenticated bodies reuse their queued job even if delivery headers
change. Accepted work survives process exit. The OS releases the worker lock
after a crash; a restarted worker resumes unfinished work. A recorded interrupted
sandbox is removed by its exact name before a new isolated test attempt. Its
unknown result is never signed. A killed supervisor can leave a container alive
until worker recovery; Docker's CPU, memory, process, and log bounds remain in
force meanwhile.

Saved model responses are reused. An uncertain model call needs an explicit
operator rereview, and an uncertain native write is reconciled without blindly
creating another check or note. Jobs requiring inspection say `attention` with
a reason. Policy changes also stop old queued jobs for inspection.

Defaults permit three attempts per job and 30 admitted job attempts per UTC day.
Retry delays increase exponentially from `retry_seconds` to `max_retry_seconds`
(one hour by default). A GitHub rate limit is not counted as an attempt: the
job waits in `retry` until the reset time GitHub reports (at most one hour).
Anonymous collection allows 60 reads an hour, so configure a read-only reader
token for sustained traffic. `reconcile_seconds` defaults to zero (disabled).
Polling queues each open PR head once; a head that another job already tested,
or a move of only the target branch, does not queue it again.
The daily limit survives restart; remaining jobs wait for the next day. Per-call
review time and budget limits still apply. `cirra jobs` shows results and today's
admission count. The queue retains up to 10,000 unique accepted bodies, after
which ingress returns an error until an operator archives completed work.
State, source archives, and bounded logs are retained for inspection; arrange
backups and retention appropriate to the repository data.

`cirra health --state-dir <state>` prints one JSON object and exits 1 when
something needs an operator: the worker heartbeat is missing or older than
`--max-heartbeat-age` (1200 s by default, because a bounded test and review can
hold the loop), a job entered `attention` within `--attention-window` (one day),
the last reconcile reported a route or branch error, today's admission is
exhausted, or a queue record is unreadable (the worker skips such records).
`examples/systemd/cirra-health.service` and `.timer` run it every five minutes;
point the service's `OnFailure=` at your alerting.

The receiver closes any connection that has not finished its request within
10 seconds, so slow clients cannot hold its 16 handler slots. It writes one
JSON line to stderr per response, with route path, status, delivery ID and
declared size, never headers, bodies or credentials, and logs only the type
name of unexpected exceptions.

## lugos-host deployment and acceptance

On 2026-10-16 the trusted release `3ccb04649589e1d45620f1309d37ce70c9757afb`
was installed on `lugos-host`. Its runtime is selected by
`/srv/lugos/services/cirra/current`; protected operator configuration lives in
`/srv/lugos/services/cirra/config` and durable state in `/srv/lugos/state/cirra`.
The `cirra-webhook.service` and `cirra-worker.service` units are enabled at boot.

Observed results:

- All 154 tests passed on the Linux host, including real Docker success,
  failure, timeout cleanup, and isolation tests under the service's private umask.
- The independent observer fetched and tested the exact head of
  [PR #7](https://github.com/mdn87/cirra/pull/7), then published the successful
  [native test check](https://github.com/mdn87/cirra/runs/116804924828).
  Recollection verified its signed checkout proof, including commit, parents,
  execution definition, and check identity.
- The running HTTP receiver rejected an unsigned request with HTTP 401,
  persisted a locally generated signed PR refresh with HTTP 202, and recognized
  its replay without creating another job. This was an operator-generated
  delivery, not evidence of public GitHub delivery.
- [PR #8](https://github.com/mdn87/cirra/pull/8) then exercised the complete public
  flow. GitHub's real PR event received HTTP 202; the worker tested commit
  `99260620b9ed5862f042f26465c3deafd60aa05f`, verified its checkout evidence, ran a
  tool-free Claude Sonnet 5.5 review, and published the successful
  [test check](https://github.com/mdn87/cirra/runs/116811075150) and neutral
  [advisory review](https://github.com/mdn87/cirra/runs/116811123077).
  The report was complete, with verified CI and zero findings.
- After restarting the worker, GitHub redelivered the same event. The receiver
  returned the original job ID with `duplicate: true`; its attempt count stayed
  at one and no additional review report was created.

The repository webhook is configured for pull-request events at
`https://lugos-host.tail099c37.ts.net:8443/hooks/cirra-github`, with TLS verification
and a separate HMAC secret. Tailscale Funnel now forwards public HTTPS port 8443
to the loopback receiver. Public health requests return HTTP 200, unsigned
deliveries return HTTP 401, and GitHub's authenticated ping returns HTTP 202.
The existing HTTPS service on port 443 remains private to the tailnet.

The operator completed Claude subscription login on the host. Both Cirra
services are running and enabled at boot. If the login expires, renew it with:

```text
ssh lugos-host
/home/matt/.local/bin/claude auth login
```

The service can run CI even while the reviewer login needs renewal. The public
repository currently uses anonymous collection; configure a separate read-only reader credential for
sustained traffic to avoid anonymous API rate limits. Opening or updating an
open, non-draft PR queues its current revision for isolated tests and review.

### GitLab observer rollout

[PR #9](https://github.com/mdn87/cirra/pull/9) adds GitLab independent execution.
The deployed runtime is now `bc7939200ea846a78853f1bcdafa0b04db24c67e`; the
remaining PR commit adds a regression test and does not change runtime source.
All 160 tests passed on lugos-host, including three real Docker checks. The
additional target-refresh regression passed locally and in all four native CI
jobs. It proves a target-only change reuses the head execution and signs a new
receipt for the refreshed snapshot.

Both `cirra-github` and `cirra-gitlab` routes are configured. The GitLab route
uses its own README-heading check and a separate execution-definition digest.
Webhook `91625689` subscribes only to Merge request events, with TLS verification
and an independent secret, at
`https://lugos-host.tail099c37.ts.net:8443/hooks/cirra-gitlab`.
GitLab's test delivery reached the receiver and created a durable worker job.
Public health returned HTTP 200 and an unsigned GitLab request returned HTTP 401.

The [sandbox MR !2](https://gitlab.com/mdn87/cirra-sandbox/-/merge_requests/2)
uses head `c00a1992bb259577f13ec653de0e831c22efa38d`. Both native pipelines
`2953727183` and `2953726032` passed. Git downloads initially returned HTTP 403,
and the worker stopped after three bounded attempts. On 2026-10-16 the operator
provided a replacement reader with Code Download. After verifying API and Git
reads, the host reader was replaced, the failed delivery was archived and
requeued, and the worker restarted. The publisher and witness key were unchanged.

The recovered provider delivery completed on its first new attempt. The isolated
README-heading test exited 0, its signed version 2 receipt verified against the
current snapshot and execution definition, and Claude returned zero findings.
Run `b56dccee200a4cf8b9596dcd79e590c3` reports `complete`, CI `verified`, and
review `succeeded` in [note 4026211013](https://gitlab.com/mdn87/cirra-sandbox/-/merge_requests/2#note_4026211013).
The native GitLab jobs remain `associated_only`; the receipt authenticates only
Cirra's independent execution.

A separate operator-generated authenticated refresh through the public endpoint
returned HTTP 202, reused the same model attempt and Docker execution, and updated
the same note to generation 2. Sending that identical payload again returned
`duplicate: true` and created no additional job. This repeat was an operator test,
not a second delivery from GitLab. Before/after API reads confirmed unchanged
pipeline and job IDs/statuses, merge eligibility and the required-pipeline setting.

Repository-owner access then merged MR !2 as
`167404555d1d9cdaeec22da951b6dd598d9723d6`.
[Main pipeline 2953821104](https://gitlab.com/mdn87/cirra-sandbox/-/pipelines/2953821104)
passed, and GitLab deleted the merged source branch. Both services remained
active and public health returned HTTP 200.
Local recovery, publication, repeat-event and merge evidence is retained under
`.cirra/deploy/gitlab-observer/`; no credentials are included in tracked evidence.

After the rollout, a real GitHub PR edit exercised the updated service on
head `e104fd0f212c7b4a849a4aa332a06fcb669cfb51`. Run
`18928154b26c45bc894b41ebe5807519` completed with verified CI, a successful
review, zero findings, and [advisory check 116822006340](https://github.com/mdn87/cirra/runs/116822006340).
The previous runtime and protected configuration backup remain available for
rollback. No database schema changed.

### Backup CI rollout

On 2026-10-17 release `5263626be17d14efb1bdd364c3123de0d6fdb798` (this PR,
with `main` merged in) replaced `bc7939200ea846a78853f1bcdafa0b04db24c67e`.
All 191 tests passed on lugos-host, including the three real Docker checks
against the deployed observer image `sha256:47826cde8764…`. The operator
configuration gained `worker.reconcile_seconds: 900` and `branches: [main]`
on the `cirra-github` route. The prior release and a configuration backup
(`staging/config-before-5263626`) remain for rollback.

The release changes the observer supervisor, so both observer definition
digests changed: GitHub `06d28457…` to `e5532136…`, GitLab `bdb34ea0…` to
`20d07506…`. The first branch job started before the review policies were
re-pinned. The worker refused it with "review policy must pin the observer
source, check name, and definition digest", exhausted three attempts, and
left it in `attention` without running or publishing anything. After the pins
were updated and the services restarted, reconcile queued a fresh job.
Recompute the digests with `cirra observer-definition` for every release that
touches observer code, before restarting.

Observed results:

- Branch CI: reconcile queued `main` at
  `b8e98e9a854d577c460af1837e6f5079b7f2ecd9`. The isolated tests succeeded,
  the signed witness receipt was recorded, and the
  [native check 117316369284](https://github.com/mdn87/cirra/runs/117316369284)
  was published with review disabled.
- PR flow: marking this PR ready produced a `ready_for_review` delivery
  (HTTP 202). The worker tested head `5263626` with a verified checkout,
  published [isolated tests 117316664098](https://github.com/mdn87/cirra/runs/117316664098),
  and a complete Claude review as
  [advisory check 117316945160](https://github.com/mdn87/cirra/runs/117316945160)
  with two low findings and one informational note, recorded on the PR.
- Heartbeat: `worker-status.json` is rewritten in place on each pass. No
  `history/worker-status` directory exists in the service state.

### Audit follow-up rollout

Later on 2026-10-17, `scripts/deploy-release.sh` deployed
`c080b1fface9e2ca7fa3c45e6749c89499ca2668` (#12). The observer digests did not
change, so the pins needed no update. A second run of the script overlapped the
first: one run rebuilt the release directory under the other's test run, which
failed (exit 4) while the other switched and restarted. #13 adds a host-side
lock so this cannot recur. The live release was then verified directly: both
services active, `/healthz` 200, both pins current, configuration unchanged,
and each response logged as one JSON line.

The worker unit was then replaced from the template. It no longer runs the
Claude login check in `ExecStartPre`, and it uses `ProtectHome=tmpfs` with only
the Claude login and CLI bound. Inside the running worker's mount namespace the
home directory contains `.claude`, `.claude.json` and `.local`, and `~/.ssh` is
not readable. `cirra-health.timer` runs every five minutes, with
`OnFailure=cirra-health-alert.service` posting an `operator_attention.requested`
event to lugos event-ingest, which sends a Pushover alert. The alert unit's
token loads from `/etc/lugos/event-ingest.env`; no test alert was sent.

Observed results:

- Rate limits: a PR job that hit the anonymous GitHub limit stayed in `retry`
  with no attempt counted until the reset, then ran. Under the previous
  release, the same condition parked a job in `attention` after three
  attempts in four minutes.
- The two jobs parked in `attention` were requeued. The branch job then
  tested `main` at `137b078` successfully under the hardened unit, and the
  other was skipped because its PR had merged.
- `cirra health` reported healthy (exit 0), and the timer's first run
  succeeded.

### Host recovery facts

Checked on lugos-host on 2026-10-17 for the production audit:

- Backups: `lugos-host-backup.timer` archives `/srv/lugos` nightly to
  `/srv/lugos/backups` with 14-day retention; the last run succeeded. The
  archive includes `state/cirra` (the SQLite review state and the signed
  witness and publication receipts), but its secret filter leaves out
  `services/cirra/config`, which holds the witness key and tokens.
- Disaster recovery: those archives are on the same disk, and the only
  off-host copy (restic to nodens) covers a different service. Losing the disk
  loses Cirra's receipts and witness key. Reviews can be re-run and
  publications reconciled from the providers, but past receipts cannot be
  re-signed. An off-host copy of `state/cirra`, and an offline copy of the
  witness key, would close this. On 2026-10-19 the whole
  `services/cirra/config` directory (12 files, including the witness key,
  the App key and the tokens) was copied off the host as an AES-256
  encrypted archive, verified against the host by SHA-256 of the
  uncompressed tar, and is kept outside lugos-host with its passphrase
  stored separately. The off-host copy of `state/cirra` remains open.
- Encryption at rest: the root filesystem is unencrypted ext4. State written
  under `UMask=0077` includes diffs from the private GitLab sandbox.
- Ingress: Tailscale Funnel on port 8443 proxies directly to the loopback
  receiver. The receiver's own 10-second total request deadline and 1 MiB body
  limit apply regardless of what Funnel enforces.

### Deploying and rolling back

`scripts/deploy-release.sh` replaces the manual steps above. From a checkout
on the operator machine:

```text
scripts/deploy-release.sh --host lugos-host --ref <commit>
scripts/deploy-release.sh --host lugos-host --ref <commit> --repin
scripts/deploy-release.sh --host lugos-host --rollback <previous-sha>
```

A deploy archives the commit, installs it as a new release from the hashed
`requirements/dev.txt` lock, runs the full suite on the host with the deployed
observer image, and saves `staging/config-before-<sha>`. It then runs
`cirra observer-pins`, which fails when a release changes the observer
definition digest. Review the release, then rerun with `--repin` to update the
pins (each policy keeps a `.before-repin` copy). The script switches `current`
atomically, restarts both services and checks `/healthz`. If they do not come
up, it restores the saved configuration and the previous release. Old
releases beyond `--keep` (3 by default) are removed; the current and previous
releases are always kept. A rollback restores the configuration saved before
the current release and switches back.

To inspect the deployment without printing credentials:

```text
systemctl status cirra-webhook.service cirra-worker.service
/srv/lugos/services/cirra/current/.venv/bin/cirra jobs --state-dir /srv/lugos/state/cirra
```

References: [GitHub webhook guidance](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks),
[Docker runtime controls](https://docs.docker.com/engine/containers/run/), and
[Tailscale Funnel](https://tailscale.com/kb/1311/tailscale-funnel).
