# Automated review and provider coordination

Cirra captures provider evidence, runs a bounded reviewer, and publishes an
advisory result. GitHub, GitLab and Forgejo retain repositories, permissions,
native workflows, runners and merge decisions.

For an always-running receiver/worker and independently observed Docker tests,
see the [service workflow](service-workflow.md). The sections below also cover
the original operator-invoked commands and their acceptance history.

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
    "evidence": "<independent observation reference>",
    "definition_digest": "<SHA-256 of the independently observed native CI definition>"
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
For release use, each expected check in the operator policy must include
`definition_digest`: the SHA-256 of its trusted native CI definition, approved
independently of the change. The witness must attest the actual definition's
same digest. Missing/mismatched definitions cannot satisfy that policy. Checkout
verification alone says nothing about whether a PR replaced tests with a no-op.
Receipts from another attempt/state cannot satisfy a newer check. A changed
target invalidates old synthetic merge parentage through the normal policy.

## GitHub advisory publication

Install a GitHub App on the selected repository with **Checks: write** and
**Metadata: read**. Record the numeric App ID and installation ID separately;
the installation ID is not a secret key, and the App ID is not the Client ID.
Keep the downloaded private PEM file under the ignored `.cirra/credentials/`
directory. PEM files are also ignored throughout this repository. Load the file
contents into a process environment variable when publishing; do not commit the
key or paste its contents into chat. Existing `.env` files are not implicitly loaded.
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
snapshots, changed policy, or a superseded local attempt. GitHub same-head rereviews
and base changes use one owned slot; a new head gets a new check slot. GitLab
keeps one maintained note per MR across head changes. The check is
matched by SHA, App, name and external slot marker. GitLab notes use the author
and slot marker. Report text neutralizes mentions, links and GitLab quick actions.

Intent and acknowledgement receipts live under `.cirra/publication/`, with
immutable history. A process-lifetime OS lock serializes each change while short SQLite
transactions fence checkpoints; remote I/O holds no database-wide write lock.
Unrelated reviews can keep renewing their claims. No database migration is required.
Process crashes are recoverable. This does not promise exactly-once remote
delivery, atomicity with a concurrent push, or coordination across independent
state directories. Filesystem/power-loss durability depends on the platform.

```text
cirra publish <same arguments> --reconcile
cirra publish <same arguments> --retry-uncertain
cirra publish <same arguments> --canonical-id <verified-owned-object-id>
```

After a lost acknowledgement, no matching object or multiple matches remain
unresolved. A definitive refusal is `failed` and permits an ordinary retry; a
local size refusal occurs before the write intent. An unconfirmed update to a
known ID can be explicitly retried without claiming duplicate-create risk. An
older remote read alone does not prove that an update failed. Reconciliation performs reads and adopts a unique acknowledged
intent. `--retry-uncertain` explicitly accepts residual duplicate risk for one
more write after inspection. Multiple matches require a verified canonical ID. That selection persists. A
previously acknowledged object that disappears becomes uncertain; after
inspection, the explicit retry can create a replacement with duplicate risk recorded.

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

The replacement sandbox reader token now supports the branch and MR diff-version
reads required by collection. [Sandbox MR !1](https://gitlab.com/mdn87/cirra-sandbox/-/merge_requests/1)
has been collected, reviewed automatically and merged. A separate scoped token
published one maintained note using the operator-confirmed permission receipt.
The account's identity verification is complete and native pipelines now run.
See the live results below for the exact limits.

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
header before parsing the bounded payload. It stores delivery IDs, detects conflicting reuse, and deduplicates authenticated
payload digests as well. Delivery headers themselves are not covered by body HMACs. Events supply only repository/change hints: Cirra fetches
current state, ignores payload SHAs/diffs, and saves a snapshot. A repeated
delivery reuses its receipt. Existing webhook infrastructure must deliver the
raw body and headers; Cirra does not run a public web server or job scheduler.
Review the resulting snapshot with the normal `review` command.

## Native release workflow requests

```text
cirra plan-release --run <run-id> --workflow-id <native-workflow-id> --ref <protected-target-branch> --output <request.json>
cirra dispatch-release --request <request.json> --config <policy> --app-id <app-id> --installation-id <installation-id> --app-key-env CIRRA_RELEASE_APP_KEY --provenance <receipt.json> --witness-key-env CIRRA_WITNESS_KEY
```

Planning requires complete captured GitHub evidence, verified successful CI
including trusted definition digests, and a successful review with no findings.
Dispatch requires the PR to be merged into the selected protected target branch
and its reviewed head to be reachable from that branch. The workflow definition
runs from the protected branch; the candidate checkout is the reviewed SHA.
Squash/rebase merges that do not preserve that head's ancestry require a new
review of the release candidate; this prototype refuses those requests. It mints a separate repository-scoped token
with Actions write and Metadata read; the reader handles commit lookup.
The native workflow must accept `cirra_request_id` and `cirra_expected_sha`,
name its run `cirra-release-<request-id>`, explicitly checkout the expected SHA,
and enforce its own release approvals. `examples/release-request.yml` illustrates
the contract; it only records evidence and does not deploy or publish a package.

The request identity excludes timestamps, so replanning the same evidence does
not bypass duplicate protection. Accepted requests are not completed releases. Durable receipts and
`--reconcile` identify the native run; a lost acknowledgement never causes an
automatic second dispatch. `--retry-uncertain` permits one explicit retry of an unacknowledged request after
inspection; it never retries an acknowledged dispatch. No live release has been dispatched by this implementation session.

## Live acceptance results

Recorded on 2026-10-15 America/New_York (2026-10-16 UTC). These are observed
results from the live providers; the implementation plan retains acceptance
requirements that still need external setup.

### GitHub

[PR #5](https://github.com/mdn87/cirra/pull/5) is merged. Its final reviewed head
was `66af347998f4ec3e3fa1f0485659b86fbd01f982` against target
`f1c10f8fb9e02bf88ba2878e8b5e0878759b5e7b`.
[Native CI](https://github.com/mdn87/cirra/actions/runs/38839480302) passed all
134 tests, lint and formatting on Windows/Linux with Python 3.11/3.14.

Subsequent runs on `main` exposed test-only timing assumptions: 200–600 ms leases
could expire during setup on shared runners. Commit `14a8cca` uses a controlled
clock for the expiry test and gives real child processes a five-second lease.
Their reviewer still runs longer than that lease, so heartbeat coverage remains.
[Native CI for that correction](https://github.com/mdn87/cirra/actions/runs/38841307296)
passed all four jobs; production lease behavior was unchanged.

| Live case | Observed result |
| --- | --- |
| Process exit before create | Durable intent remained uncertain; read-only reconciliation found no remote check and made no write. |
| Process exit after the remote create but before saving acknowledgement | Reconciliation recovered the unique owned check. |
| Delayed read visibility | Suppressing the discovery result left the intent uncertain without creating a duplicate. This was controlled fault injection around a real remote object. |
| Repeat publication and explicit re-review | Both retained the same check on the same head. A superseded local review attempt was refused. |
| Target branch advances with unchanged head | The live case exposed stale GitHub PR `base.sha` metadata. After the fix, Cirra refused the old-target report; a fresh review updated the same check. |
| Head advances | A fresh review created a new check for the new commit. Read-only CLI reconciliation recovered that exact check. |
| Older-target report arrives after newer-target publication | PR #6 retained one check with identical output and ownership metadata after the old report was refused. |

The original-head check is
[`116772324382`](https://github.com/mdn87/cirra/runs/116772324382); the final-head
check is [`116776873598`](https://github.com/mdn87/cirra/runs/116776873598).
Both belong to App `5339263` and are neutral. All four automatic reviewer
attempts on PR #5 succeeded with no findings. Publication and recovery left saved model
responses unchanged; they did not invoke the model again.

The target-change defect was reproduced before the fix: GitHub's PR metadata
still reported target `2fb685a` after `main` advanced to `f1c10f8`, so an outdated
report could be republished. Cirra now independently resolves the target branch
before and after collection and refuses failed or inconsistent reads. Regression
coverage also checks branch movement, denied/malformed reads, encoded names and
fork target identity. The actual live fork scenario still needs a controlled
fork under another owner.

[PR #6](https://github.com/mdn87/cirra/pull/6) exercised the delayed-report case
on unchanged head `cd29046d9efe8f871ab9da0a8ff2aa54d6b3a4bc`. The first review
captured target `14a8cca`; after `main` advanced to `1e3b136`, a fresh review was
published as [check 116786898954](https://github.com/mdn87/cirra/runs/116786898954).
Trying to publish the older report afterward was refused. Before/after reads
confirmed one owned check, the same native ID and marker, and identical output
hashes. Both reviews succeeded with no findings; publication did not invoke the
model again. The receipt is `cases/delayed-target-ordering.json` in the local
acceptance directory.

Fault injection used the saved capture and a live revision check around the
publisher; final ordinary CLI publication performed fresh collection. Local
case receipts and response hashes are under `.cirra/github-acceptance/cases/`.
No independent checkout witness is configured, so native CI success remains
association evidence and the Cirra reports remain `incomplete`.

### GitLab

The private [sandbox MR !1](https://gitlab.com/mdn87/cirra-sandbox/-/merge_requests/1)
adds the README and a native `test` job. Its final reviewed head was
`8f41554687c46a998114f0895b3c0ac5fbfcdfc5`, against target
`652a4c8d39a048ba918fe06c0e4f992c1d2f9013`. Repository-owner credentials merged
it as `e79cff5f0a4685c0fd4d0a72b54d1150452a2032`; the note publisher has no merge
or repository-write permission.

The initial README review `051f553198614931a13bca84d5eec5e3` succeeded with no
findings and was reused on repeat invocation. At that point GitLab required
account identity verification: pipelines `2953418816` and `2953420191` failed
with zero jobs. The operator completed verification and supplied a different
publisher token, confirming project-only Work Item Create/Read/Update permissions
and no code-write or merge permissions. A local token-bound receipt records that
operator assertion; it is not server attestation of token scope.

The native README test initially failed on CRLF line endings. After correcting
the check to accept LF and CRLF, both final-head pipelines passed:

| Native pipeline | Job | Result |
| --- | --- | --- |
| [MR pipeline 2953467071](https://gitlab.com/mdn87/cirra-sandbox/-/pipelines/2953467071) | `test`, ID `17220571846` | success |
| [Push pipeline 2953467045](https://gitlab.com/mdn87/cirra-sandbox/-/pipelines/2953467045) | `secret_detection`, ID `17220571675` | success |

After the merge, [main pipeline 2953471825](https://gitlab.com/mdn87/cirra-sandbox/-/pipelines/2953471825)
also passed both jobs, and GitLab deleted the merged source branch.

The sandbox now requires successful pipelines before merging. While the test
failed, GitLab reported `detailed_merge_status: ci_must_pass`. With the corrected
head it became mergeable. Cirra refused publication of the obsolete-head report;
fresh review `0c08e81b72e2436a9ce0f99b1bc3cc39` returned no findings.

Initial publication, repeat publication, read-only reconciliation and the new
head all maintained [note 4025547155](https://gitlab.com/mdn87/cirra-sandbox/-/merge_requests/1#note_4025547155).
Only one owned note existed, reaching generation 4. Before/after captures proved
unchanged pipeline and job IDs/statuses, merge eligibility, and the enabled
`only_allow_merge_if_pipeline_succeeds` setting. Saved model-response hashes
were unchanged by publication. No commit status or external pipeline was created.

The final report remains `incomplete`: native success is `associated_only`
until an independent checkout witness is available. The reviewer also flagged
uncertainty about a `stages` declaration outside the supplied diff; direct file
inspection and the successful native job confirmed that `test` is declared.
This demonstrates the limits of review from bounded diff context.

Local captures, reports, operator receipt, before/after API evidence and screenshots
are under `.cirra/gitlab-demo/`. No credentials are included in tracked evidence.

The subsequent [sandbox MR !2](https://gitlab.com/mdn87/cirra-sandbox/-/merge_requests/2)
completed independent checkout observation on lugos-host after replacing the
reader with a token that permits Code Download. Run
`b56dccee200a4cf8b9596dcd79e590c3` is `complete`, with verified independent CI,
a successful review and zero findings. A repeat event reused the same Docker
execution and model attempt while updating
[note 4026211013](https://gitlab.com/mdn87/cirra-sandbox/-/merge_requests/2#note_4026211013).
Native pipelines, jobs and merge eligibility were unchanged by publication.
MR !2 is merged and its main pipeline passed. The
[service acceptance record](service-workflow.md#gitlab-observer-rollout) explains
the provider delivery, explicit recovery and operator-generated replay test.

Human usefulness assessment of the frozen reviewer cases and separate native
release setup remain open alongside the GitHub fork case above.

## References

- [GitHub Checks API](https://docs.github.com/en/rest/checks/runs)
- [GitHub installation-token restrictions](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [GitLab MR diff versions](https://docs.gitlab.com/api/merge_requests/#retrieve-a-merge-request-diff-version)
- [GitLab pipeline jobs](https://docs.gitlab.com/api/jobs/#list-pipeline-jobs)
- [GitLab MR notes](https://docs.gitlab.com/api/notes/#merge-requests)
- [GitLab fine-grained permissions](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens_rest/)
- [Forgejo live API schema](https://codeberg.org/swagger.v1.json)
