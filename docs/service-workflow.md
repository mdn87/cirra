# Automatic PR reviews and independently observed tests

An open, non-draft pull request can trigger the complete flow: an authenticated
webhook enters a durable local queue; the worker fetches the current change,
runs the selected tests in an isolated Docker container, obtains an AI review,
and publishes the results. GitHub still owns the repository, checks, branch
rules, and merge decision. Cirra never merges a PR.

Service objectives, alert ownership, publication recovery and cold storage are
documented in [Worker operations](worker-operations.md).

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
   docker build -f examples/observer.Dockerfile -t cirra-observer:local .
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
   The GitHub App key is a BWS reference. The worker mints repository-scoped
   tokens with Checks write and Metadata read. Private repository collection
   needs a separate reader token that can also fetch the Git repository.
5. Generate independent, high-entropy webhook and witness secrets of at least
   32 characters. Store them in BWS, never in the repo. The receiver manifest
   maps `CIRRA_GITHUB_WEBHOOK_SECRET`; the worker manifest maps
   `CIRRA_WITNESS_KEY`, `CIRRA_GITHUB_APP_PRIVATE_KEY_PEM`, and any configured reader or
   publisher tokens.
6. On a fresh Linux host, create the shared Cirra state directory and the private
   GLAS state directories before starting the example systemd units. The paths
   in each unit's `ReadWritePaths` must exist before systemd can start the
   receiver or worker. For the lugos-host layout, run

   ```sh
   sudo install -d -m 0700 -o matt -g matt \
     /srv/lugos/state/cirra \
     /srv/lugos/state/glas/cirra-webhook \
     /srv/lugos/state/glas/cirra-worker \
     /srv/lugos/state/glas/cirra-health \
     /srv/lugos/state/glas/cirra-health-resolve
   ```

   If your installation uses a different service account or layout, use the
   units' `User` and `Group`, the Cirra state path, and each GLAS configuration's
   `home` path.

When upgrading a version 1 service configuration that uses
`publisher.private_key_file`, first map the existing App key to
`CIRRA_GITHUB_APP_PRIVATE_KEY_PEM` in the worker's GLAS manifest. Replace
the file setting with `private_key_env: CIRRA_GITHUB_APP_PRIVATE_KEY_PEM`
in each affected GitHub route. Keep the existing witness key so previously
signed receipts still verify. Run the candidate's `cirra observer-pins
--service-config <service.yaml>` before switching releases. Hosts with the
scheduled backup also need its witness-only manifest.
[Backup setup](state-recovery.md#install-the-recovery-job)

Each service process has a separate GLAS manifest. For an interactive launch,
`GLAS_MANIFEST` selects the manifest for `scripts/with-glas.py`. `GLAS_HOME`
selects the GLAS state directory. The launcher uses the default native credential
profile. The units in `examples/systemd/` show how lugos-host runs each service.
Systemd decrypts the host machine token into its private credential directory,
and `host-runtime.py` launches GLAS with one receiver,
worker, or health manifest. The machine account must be allowed to read every
referenced BWS entry before the service is restarted. The model and sandbox do
not inherit these credentials.

The health alert and recovery units use `cirra-health.json` and
`cirra-health-resolve.json`. Both launcher configurations reference the health
manifest, which supplies `LUGOS_EVENT_AUTH_TOKEN`. Run `cirra.health_notify`
in `alert` or `resolve` mode respectively, using the same pending-alert marker.

## Start the receiver and worker

Run these as separate processes with the same state directory:

```text
GLAS_MANIFEST=<receiver-manifest> python scripts/with-glas.py serve-webhooks --service-config <service.yaml> --state-dir <state> --port 8796
GLAS_MANIFEST=<worker-manifest> python scripts/with-glas.py worker --service-config <service.yaml> --state-dir <state>
cirra jobs --state-dir <state>
```

`worker --once` handles one available job and exits. A normal worker stays
running. `examples/systemd/` contains restartable Linux service templates.
The selected account must have Docker access. Claude review uses that account's
own login; never copy another account's session credentials. The worker starts
without a Claude login so independent CI can still run. An unavailable reviewer
leaves the review needing attention while retaining the separate CI result.

### Read-only audit route

`cirra serve-audit-reads --state-dir <state> --port 8797` serves the audit
store to derived views such as Taskmaster. It is a separate process beside the
receiver and worker: it takes no service configuration and no credential file,
holds no webhook or publisher secret, and opens Cirra's state read-only.
It binds 127.0.0.1 only and refuses any other address at startup, and
every method except GET is answered 405.

Routes, all GET with JSON bodies:

- `/healthz`: `{"status": "ok", "version": ..., "audits": <count>}`.
- `/audits?limit=&after=&head=&repository_key=&tier=&state=`: a page of attempt
  summaries in store order, at most 200 per page (default 50). `next` is the
  `after` cursor of the following page, or null. An unknown or repeated query
  key or a malformed value is 400.
- `/audits/<attempt_id>`: the summary plus `identity`, `report`, `comparison`
  and `validator_output` as stored.
- `/gates/<audit_id>`: the gate as Cirra published or waived it: the
  `audit-publication/<audit_id>.json` receipt and every `audit-waivers/*.json`
  receipt naming that audit, copied verbatim. Nothing is recomputed. A release
  gate that passed leaves no receipt, so the route answers 404 until a
  publication or a waiver exists.

Every 200 response is `{"protocol": "cirra.audit-reads.v1", "schema_version":
..., "generation": ..., "record": ..., "sha256": ...}`. `sha256` is the SHA-256
of the canonical JSON of `record` (sorted keys, no spaces, ASCII), and
`generation` digests every served audit row and receipt. The reader checks
SQLite's data version and the receipt directories before and after each read.
It verifies the returned gate against the same receipt hashes. Its private,
temporary lookup index contains filenames, hashes and audit IDs. Warm health
and summary reads reuse the verified generation. A cold index keeps building
if an HTTP request reaches its six-second budget. The client can retry a 503.
A missing store, a store at another schema version or an unreadable receipt
also returns 503. Audit packets under
`audit-packets/` are not served; they carry repository content.
These GET paths, the literal `cirra.audit-reads.v1` identifier, and the response
fields form the v1 contract. A breaking change needs a separately selected version.
Every response has a server-generated `X-Request-ID` for matching the request log.
The reader emits one `audit_read` completion event per handled connection.
Its `duration_ms` includes response writes and socket cleanup. The `status`
field identifies the HTTP status selected, when known. Use `outcome` to
distinguish a completed response from a disconnect or an internal failure.

The optional systemd service uses the dedicated `cirra-audit-reader` account.
Create that system account and group once, then run
`bash scripts/grant-audit-read-access.sh /srv/lugos/state/cirra` as the state
owner. The helper needs POSIX ACL support and an existing SQLite database.
Reapply it when restoring state to new inodes. It grants traversal of the state
root, read access to the database, and inherited read access to publication
and waiver files. The unit mounts the state and installed virtual environment
read-only under `/run`, hides the host service tree and receipt histories, and
binds only port 8797 on loopback. The installed virtual environment must be
readable and traversable by the reader. Restart the reader after a release
switch so its runtime mount follows the selected release.

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
GLAS_MANIFEST=<worker-manifest> python scripts/with-glas.py sync --service-config <service.yaml> --state-dir <state>
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

For the private sandbox reader, select only the sandbox project. Grant **Read**
on **Project**, **Branch**, **Merge Request**, and **Pipeline** for collection,
and **Code → Download** for Git-over-HTTPS. Reading the raw CI job log also
requires **Job → Read**. GitLab checks that permission separately from the
permission to list jobs in a pipeline. [GitLab REST permissions](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens_rest/#job)

The publisher uses its own project-scoped `GITLAB_PUBLISHER_TOKEN` with Work
Item Create/Read/Update and an explicit operator permission receipt. Neither
the observer nor the publisher needs
repository-write or merge permission. The webhook uses Merge request events,
TLS verification and `CIRRA_GITLAB_WEBHOOK_SECRET` at `/hooks/cirra-gitlab`.

To change reader permissions, generate a fine-grained token with the required
selections. In a Glas deployment, edit the **Value** of the existing Bitwarden
secret identified by the worker's `GITLAB_TOKEN` reference. Check the secret ID
when names match. The machine account can have different access to each entry.
[Bitwarden secret access](https://bitwarden.com/help/secrets/#assign-secrets-to-people-or-machine-accounts)

Verify that the worker's machine account can load the reader, read the project
and fetch the branch. If job logs are required, also read a nonempty trace.
Restart the worker and check its next reconciliation and health result. A
delivery that exhausted its retries needs explicit operator recovery with its
prior record retained. Verify the replacement before revoking the previous
token. GitLab's Rotate operation retains the current permissions. [GitLab token rotation](https://docs.gitlab.com/user/profile/personal_access_tokens/#rotate-a-personal-access-token)

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
(one hour by default). When GitHub or GitLab supplies a later reset time, the
worker waits until that time. A quota refusal before execution returns the
job's daily admission.
Anonymous collection allows 60 reads an hour, so configure a read-only reader
token for sustained traffic. `reconcile_seconds` defaults to zero (disabled).
Polling queues each open PR head once; a head that another job already tested,
or a move of only the target branch, does not queue it again.
The daily limit survives restart; remaining jobs wait for the next day. Per-call
review time and budget limits still apply. `cirra jobs` shows results and today's
admission count. The review queue has 10,000 slots by default. At capacity,
new deliveries and operator retries of archived jobs can reclaim one eligible
terminal job. If all slots are occupied by ineligible work, the queue refuses
new admissions until a slot opens. The worker also archives old terminal records.
State, source archives, and bounded logs are retained for inspection; arrange
backups and retention appropriate to the repository data.

`cirra health --state-dir <state>` prints one JSON object and exits 1 when
something needs an operator: the worker heartbeat is missing or older than
`--max-heartbeat-age` (1200 s by default, because a bounded test and review can
hold the loop), a job entered `attention` within `--attention-window` (one day),
the last reconcile reported a route or branch error, today's admission is
exhausted, or a queue record is unreadable (the worker skips such records).
`examples/systemd/cirra-health.service` and `.timer` run it every five minutes.
`cirra-health-alert.service` (`OnFailure=`) posts an
`operator_attention.requested` event to lugos event-ingest, and
`cirra-health-resolve.service` (`OnSuccess=`) resolves it on the next healthy
run. Both units use HTTP for loopback and verified HTTPS for a remote recipient
by default. On lugos-host, event-ingest listens on the same host's private
`10.0.1.33:8787` interface with HTTP. Set `LUGOS_EVENT_SCHEME=http` in both
health notification units for that route. Cirra checks that both ends of the
TCP connection have that local address before sending the event credential.
Use verified HTTPS for recipients on another host, including on the same LAN.
The alert uses fallback text if the journal takes more than three seconds.
A rejected resolution keeps the incident marker for the next healthy run.

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
token loads from `/etc/lugos/event-ingest.env`. The chain was proven in
production rather than by a test: on 2026-10-17 the health check failed with
`daily admission exhausted (30)` during the deployment windows, the alert
unit posted 92 requests under dedupe key `cirra-health-lugos-host`, and the
attention daemon recorded two Pushover deliveries with status `sent` (18:12Z
and 18:42Z) that reached the operator's phone. The request stayed `active`
afterwards because nothing resolved it on recovery and the event carried no
`ttl_seconds`; while a request is active the daemon suppresses repeat pushes
at the same urgency, so the next incident would not have pushed. It was closed
by hand on 2026-10-19 with an `operator_attention.resolved` event (same `id`
as the active request, reason `operator_resolved`).

Since 2026-10-19 the health unit also carries
`OnSuccess=cirra-health-resolve.service`. The alert unit writes
`health-alert.pending` (the generated request id) under the state directory
and sends `ttl_seconds: 86400`, so an unresolved request expires a day after
the last failed run. If `LUGOS_EVENT_AUTH_TOKEN` is missing or empty, the
alert unit exits with an error before writing the marker or contacting
event-ingest. The resolve unit runs only while that marker exists
(`ConditionPathExists=`), posts `operator_attention.resolved` with reason
`run_succeeded` and no `id` (the daemon resolves the active request for the
dedupe key; request ids are primary keys and cannot be reused across
incidents), and removes the marker once event-ingest accepts the event. A
failed post leaves the marker for the next healthy run, and so does a missing
token: running the resolve command with an empty `LUGOS_EVENT_AUTH_TOKEN` on
2026-10-19 exited 1 with `marker kept for the next healthy run` and left the
marker in place. All three units live
in `examples/systemd/`; `scripts/deploy-release.sh` does not install unit
files, so a unit change is deployed with
`sudo install -m 0644 examples/systemd/cirra-health*.service /etc/systemd/system/`
followed by `sudo systemctl daemon-reload`. The resolve leg was proven on
2026-10-19: with the marker present, one healthy run started the resolve
unit, which logged `resolve posted: HTTP 202`; the marker was gone, the
attention daemon logged the resolved event for `cirra-health-lugos-host` two
seconds later, and the attention database held no active row for that key.
The failure leg (alert followed by automatic resolve) has not been exercised
end to end; the alert payload is unchanged apart from `ttl_seconds` and the
marker write. The installed drop-in `cirra-health.service.d/alert.conf` still
carries the original `OnFailure=`, now a duplicate of the unit's own line.

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

### Audit read route rollout

The read-only audit route (`serve-audit-reads`, above) was added on 2026-10-19
and is not yet deployed; nothing on the host has changed. The host change, when
the operator makes it, is:

```text
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin cirra-audit-reader
bash scripts/grant-audit-read-access.sh /srv/lugos/state/cirra
sudo install -m 0644 examples/systemd/cirra-audit-reads.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cirra-audit-reads.service
curl -s http://127.0.0.1:8797/healthz
```

The unit listens on loopback port 8797 beside the receiver on port 8796.
Taskmaster reads the route on the same host. The deployment script restarts
the audit reader when it is active and checks its systemd status during
readiness verification. See `taskmaster/docs/work-view-concept.md`, section 11.

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

The November 20 recovery drill restored a Cirra-only snapshot from encrypted
off-host storage, passed SQLite integrity and foreign-key checks, and verified
the current and historical observer signatures with the separately escrowed key.
The six-hour online backup timer repeats the isolated restore. The [state recovery
guide](state-recovery.md) covers installation, restore commands and live-storage
encryption. Production SQLite and the local host archives still use plaintext
storage.

### Deploying and rolling back

`scripts/deploy-release.sh` replaces the manual steps above. From a checkout
on the operator machine:

```text
scripts/deploy-release.sh --host lugos-host --ref <commit>
scripts/deploy-release.sh --host lugos-host --ref <commit> --repin
scripts/deploy-release.sh --host lugos-host --ref <commit> --fallback <installed-full-sha>
scripts/deploy-release.sh --host lugos-host --rollback <previous-sha>
```

A deploy archives the commit, installs it as a new release from the hashed
`requirements/dev.txt` lock, runs the full suite on the host with the deployed
observer image, and saves `staging/config-before-<sha>`. It then runs
`cirra observer-pins`, which fails when a release changes the observer
definition digest. Review the release, then rerun with `--repin` to update the
pins (each policy keeps a `.before-repin` copy). The script switches `current`
atomically and restarts the receiver, worker, and any active audit reader or
health timer. Promotion requires every unit to be active, a healthy receiver
and a heartbeat from the new worker. When the audit reader is active, its
database-backed health endpoint must also return HTTP 200. Set
`CIRRA_AUDIT_READ_PORT` to the configured port if it differs from 8797.
The heartbeat must identify the current
systemd invocation and a live process belonging to the unit's main process
or its descendants. This supports hosts with a credential launcher.
Installed unit templates are synchronized
with the release. Operator drop-ins remain in place. If a unit has local edits,
the script names it so the operator can reconcile those edits before switching.
An interrupted installation is retained under staging and rebuilt on retry.

Before promotion or rollback the script reads the existing SQLite schema
and checks compatibility with the selected binary. If it differs, keep the
database and use a compatible binary. For a planned schema transition, first
rehearse the change and recovery on an isolated, consistent copy. Preserve
work accepted after the backup. A failed restart restores the saved
configuration and previous release when it can read both the database and the
live worker queue.

For a first rollout of a newer queue format, stage and independently test a
protected, reviewed fallback release under its full commit SHA. Supply that SHA
with `--fallback`. Before switching, the deployment checks its completed
installation, SQLite schema, support for the queue format the candidate can
write, and observer pins against a private copy of the current configuration.
Any observer policy changed by `--repin` must be recoverable from that copy.
If startup fails after the candidate advances the queue format, the script
restores that configuration, selects the compatible fallback and checks its
readiness and application health. If the previous release still reads the live
state, it takes precedence.
The failed deployment exits nonzero even when recovery succeeds.

On hosts with an active audit reader, choose a fallback with the same dedicated
account and read-only mounts. Historical templates ran the listener under the
worker account. Stop the optional reader before a rollback to those templates
and restore its access restrictions before enabling it again.

After a successful deployment, the script prints a Bash command to roll back
the selected host to a compatible release. Run it from the operator checkout
with the same path and port settings used for deployment. The script keeps
the current, previous and explicitly selected fallback directories when
pruning other releases beyond `--keep` (3 by default). A manual rollback
to the selected fallback reuses the configuration verified for this deployment.

To inspect the deployment without printing credentials:

```text
systemctl status cirra-webhook.service cirra-worker.service
/srv/lugos/services/cirra/current/.venv/bin/cirra jobs --state-dir /srv/lugos/state/cirra
```

References: [GitHub webhook guidance](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks),
[Docker runtime controls](https://docs.docker.com/engine/containers/run/), and
[Tailscale Funnel](https://tailscale.com/kb/1311/tailscale-funnel).
