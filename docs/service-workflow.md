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
The selected account must already have Docker access and its own Claude login.
The worker startup check waits for that login; never copy another account's
session credentials to make it pass.

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

Native pipeline-dependent merge rules stay authoritative. Requiring the local
observer in Cirra policy does not replace those rules or attest existing native
jobs. Source fetching, Docker isolation, retry limits and review recovery are
the same as the GitHub route.

GitLab documents the [MR head ref](https://docs.gitlab.com/user/project/merge_requests/merge_request_troubleshooting/)
and the [Code Download permission for Git operations](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens_other/).

## Recovery and operating limits

Queue and observer receipts use atomic files; existing review state keeps its
SQLite schema. No state database migration is required. Run one worker and one
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
The daily limit survives restart; remaining jobs wait for the next day. Per-call
review time and budget limits still apply. `cirra jobs` shows results and today's
admission count. The queue retains up to 10,000 unique accepted bodies, after
which ingress returns an error until an operator archives completed work.
State, source archives, and bounded logs are retained for inspection; arrange
backups and retention appropriate to the repository data.

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

The service retries startup after login. The public repository currently uses
anonymous collection; configure a separate read-only reader credential for
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
`2953727183` and `2953726032` passed. Its independent checkout is **not yet
accepted**: GitLab permits API reads but returns HTTP 403 for Git downloads
using `GITLAB_TOKEN`. The user was asked to add Code Download for this project.
The worker stopped after three bounded attempts; it has not run the GitLab
tests, invoked its reviewer, or published a note for this MR. Keep the MR open
until that permission is available and the live flow can be completed.

After the rollout, a real GitHub PR edit exercised the updated service on
head `e104fd0f212c7b4a849a4aa332a06fcb669cfb51`. Run
`18928154b26c45bc894b41ebe5807519` completed with verified CI, a successful
review, zero findings, and [advisory check 116822006340](https://github.com/mdn87/cirra/runs/116822006340).
The previous runtime and protected configuration backup remain available for
rollback. No database schema changed.

To inspect the deployment without printing credentials:

```text
systemctl status cirra-webhook.service cirra-worker.service
/srv/lugos/services/cirra/current/.venv/bin/cirra jobs --state-dir /srv/lugos/state/cirra
```

References: [GitHub webhook guidance](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks),
[Docker runtime controls](https://docs.docker.com/engine/containers/run/), and
[Tailscale Funnel](https://tailscale.com/kb/1311/tailscale-funnel).
