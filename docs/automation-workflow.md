# Automated review and provider coordination

Cirra captures provider evidence, runs a bounded reviewer, and publishes an
advisory result. GitHub, GitLab and Forgejo retain repositories, permissions,
native workflows, runners and merge decisions.

## Automatic subscription review

An installed Claude Code CLI can use its existing subscription login. No new
model API key is needed. For a synthetic example:

```text
cirra review --snapshot examples/change.json --config examples/claude-policy.yaml
```

For GitHub, copy `examples/github-policy.yaml` to an operator-controlled local
file and set the reviewer to `kind: claude`, `endpoint: cli://claude`, and an
explicit model. Then collect and review in one invocation:

```text
cirra review --provider github --repo mdn87/cirra --change <number> --config <policy>
```

The native executable runs in an empty directory under the state directory.
Its built-in tools, MCP servers, customizations, hooks, Chrome integration and
session persistence are disabled. Its environment retains only runtime/login
locations; provider credentials, model API keys and endpoint overrides are not
forwarded. The trusted CLI process retains access to its subscription login;
this is a tool-free model interface, not OS isolation of a hostile CLI binary.
Cirra does not execute the changed code. The data sent to the model is the
allowlisted repository's bounded snapshot and available patches.

Timeout, response bytes, findings and the CLI's API-equivalent budget are
bounded. Usage and the session ID come from the CLI transport. Subscription
billing remains unknown; the CLI's dollar estimate is not an actual bill.
An interrupted call with no saved response becomes `uncertain`; it never
automatically spends again. `--rereview` explicitly starts another attempt.
Identical inputs reuse a saved response while refreshing CI.

## Checkout witnesses

Native success alone does not establish which revision ran. Cirra can verify
an HMAC-SHA256 receipt from an explicitly trusted, independent checkout
observer. The observer must bind repository identity, native check/run/attempt,
association, start time and final state to the actual tested SHA and parents.
`cirra.provenance.binding(change, check)` computes that binding from captured
records. A statement has this shape:

```json
{
  "version": 1,
  "witness": "operator-controlled runner observer",
  "observed_at": "2026-10-15T00:00:00Z",
  "checks": [{
    "binding": "<64-character binding digest>",
    "tested_sha": "<full checkout SHA>",
    "parents": ["<full parent SHA>"],
    "evidence": "<independent observation reference>"
  }]
}
```

```text
cirra sign-provenance --statement <statement.json> --key-env CIRRA_WITNESS_KEY --output <receipt.json>
cirra collect --provider github --repo mdn87/cirra --change <number> --config <policy> --provenance <receipt.json> --witness-key-env CIRRA_WITNESS_KEY
```

Use an independently generated secret containing at least 32 bytes. Signing is
an operator assertion of independently checked facts, not a way to upgrade an
untrusted CI artifact. Never put the witness key in a PR workflow, its tested
code, or a reviewer. An ordinary artifact uploaded after tests can be modified
by those tests and is insufficient. No independent live witness has yet been
provisioned for Cirra's hosted CI; those checks remain `associated_only`.
Receipts from another attempt/state cannot satisfy a newer check. A changed
target invalidates old synthetic merge parentage through the normal policy.

## GitHub advisory publication

Install a GitHub App on the selected repository with **Checks: write** and
**Metadata: read**. Keep its private PEM key in a local environment variable.
Install the optional signing dependency locally with `pip install 'cirra[publish]'`.

```text
cirra publish --run <run-id> --config <policy> --publisher-id <app-id> --installation-id <installation-id> --app-key-env CIRRA_GITHUB_APP_KEY
```

Cirra verifies the installation's App identity and mints an installation token
restricted to exactly the repository and those permissions. The separate reader
credential is selected with `--reader-token-env`; anonymous public reads work.
The model never receives either credential. The native check is `cirra/review`
with a neutral conclusion. Keep it out of required merge checks.

Publication refreshes CI without another model call and refuses changed
snapshots, changed policy, or a superseded local attempt. Same-head rereviews
and base changes use one owned slot; a new head gets a new slot. The check is
matched by SHA, App, name and external slot marker. GitLab notes use the author
and slot marker. Report text neutralizes mentions, links and GitLab quick actions.

Intent and acknowledgement receipts live under `.cirra/publication/`, with
immutable history. The existing SQLite claim and transaction serialize the
remote write across local processes; no database migration is required.
Process crashes are recoverable. This does not promise exactly-once remote
delivery, atomicity with a concurrent push, or coordination across independent
state directories. Filesystem/power-loss durability depends on the platform.

```text
cirra publish <same arguments> --reconcile
cirra publish <same arguments> --retry-uncertain
cirra publish <same arguments> --canonical-id <verified-owned-object-id>
```

After a lost acknowledgement, no matching object or multiple matches remain
unresolved. Reconciliation performs reads and adopts a unique acknowledged
intent. `--retry-uncertain` explicitly accepts residual duplicate risk for one
more write after inspection. Multiple matches require a verified canonical ID.

## GitLab portability

```text
cirra collect --provider gitlab --repo mdn87/cirra-sandbox --change <iid> --config examples/gitlab-policy.yaml --token-env GITLAB_READER_TOKEN
```

GitLab collection pins MR diff-version head/base/start commits and the current
target, then rechecks them. It retains pipeline IDs, job IDs, retries, raw job
states and missing evidence. An older diff-version target or limited diff makes
coverage incomplete. Pipeline jobs use `kind: pipeline_job` and the stable
project ID as source identity. Job association is not checkout proof.

Use a separate publisher token limited to reading/creating/updating MR notes,
with no merge, repository-write or protection-management permissions. Some
fine-grained tokens cannot inspect themselves or `/user`; Cirra therefore
requires a token-bound **operator-verified** setup receipt. Inspect the token's
actual selected project and permissions in GitLab before recording:

```json
{
  "version": 1,
  "instance": "https://gitlab.com",
  "repository_id": "87658566",
  "principal_id": "<native author user ID>",
  "token_sha256": "<SHA-256 of the exact publisher token>",
  "checked_at": "2026-10-15T00:00:00Z",
  "evidence": "<where the operator verified token permissions>",
  "note_read_create_update": true,
  "no_merge_or_repository_write": true
}
```

```text
cirra publish --run <run-id> --config examples/gitlab-policy.yaml --reader-token-env GITLAB_READER_TOKEN --publisher-token-env GITLAB_PUBLISHER_TOKEN --publisher-id <user-id> --permission-receipt <local-receipt.json>
```

This receipt is an explicit operator trust input, not an independently verified
server attestation. Cirra never probes forbidden mutations to discover scope.
The publisher maintains an advisory MR note; it does not create commit statuses
or external pipelines. Existing `.env` files are not implicitly loaded.

On 2026-10-15 the supplied sandbox token could read the project, MR list and
pipelines, but repository-tree reads returned HTTP 403. There was no open MR.
Repository/diff read permission, a sandbox test MR and a separate scoped note
publisher are needed to complete the private live demonstration.

## Forgejo and authenticated event intake

```text
cirra collect --provider forgejo --instance https://codeberg.org --repo forgejo/forgejo --change <number> --config <allowlisted-policy>
cirra ingest-event --provider github --headers <headers.json> --body <raw-body.json> --secret-env CIRRA_WEBHOOK_SECRET --config <policy>
```

Forgejo uses an explicit HTTPS origin, native PR diffs and commit statuses.
Quoted/ambiguous diff paths or missing patches remain coverage gaps. A status
without a native creator retains `source_id: not_exposed` and cannot satisfy
expected-source policy. Forgejo publication is not enabled.

Event intake verifies GitHub/Forgejo HMAC signatures or GitLab's secret-token
header before parsing the bounded payload. It stores delivery IDs and detects
conflicting replays. Events supply only repository/change hints: Cirra fetches
current state, ignores payload SHAs/diffs, and saves a snapshot. A repeated
delivery reuses its receipt. Existing webhook infrastructure must deliver the
raw body and headers; Cirra does not run a public web server or job scheduler.
Review the resulting snapshot with the normal `review` command.

## Native release workflow requests

```text
cirra plan-release --run <run-id> --workflow-id <native-workflow-id> --ref <branch-or-tag> --output <request.json>
cirra dispatch-release --request <request.json> --config <policy> --app-id <app-id> --installation-id <installation-id> --app-key-env CIRRA_RELEASE_APP_KEY --provenance <receipt.json> --witness-key-env CIRRA_WITNESS_KEY
```

Planning requires complete captured GitHub evidence, verified successful CI and
a successful review with no findings. Dispatch refreshes evidence and validates
the ref against the reviewed SHA. It mints a separate repository-scoped token
with Actions write and Metadata read; the reader handles commit lookup.
The native workflow must accept `cirra_request_id` and `cirra_expected_sha`,
name its run `cirra-release-<request-id>`, explicitly checkout the expected SHA,
and enforce its own release approvals. `examples/release-request.yml` illustrates
the contract; it only records evidence and does not deploy or publish a package.

Accepted requests are not completed releases. Durable receipts and
`--reconcile` identify the native run; a lost acknowledgement never causes an
automatic second dispatch. `--retry-uncertain` permits one explicit retry after
inspection. No live release has been dispatched by this implementation session.

## References

- [GitHub Checks API](https://docs.github.com/en/rest/checks/runs)
- [GitHub installation-token restrictions](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [GitLab MR diff versions](https://docs.gitlab.com/api/merge_requests/#retrieve-a-merge-request-diff-version)
- [GitLab pipeline jobs](https://docs.gitlab.com/api/jobs/#list-pipeline-jobs)
- [GitLab MR notes](https://docs.gitlab.com/api/notes/#merge-requests)
- [GitLab fine-grained permissions](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens_rest/)
- [Forgejo live API schema](https://codeberg.org/swagger.v1.json)
