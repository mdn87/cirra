# GitHub App publishing setup

Cirra uses a GitHub App to publish an advisory `cirra/review` check on the
reviewed commit. The App's identity and publishing credential are separate
from the credential used to read a change. The reviewer receives neither.

## Register and install the publisher

Create a GitHub App under the account that owns the selected repository. Give
it **Checks: read and write** and **Metadata: read-only**, with no other
permissions. Disable webhooks for this command-line setup. Leave the callback
and setup URLs empty, and do not request user authorization during installation.

Choose **Only on this account**, install the App with **Only select
repositories**, and select the intended repository. Record the numeric App ID
and installation ID. The App ID is different from the OAuth Client ID; the
installation ID identifies the installation and is not a secret.

Generate a private PEM key in the App settings and store the downloaded file
under `.cirra/credentials/`. That directory and all `*.pem` files are ignored
by this repository. Keep private key contents out of chat, command arguments,
tracked files, and review input.

Install the optional `publish` dependencies in the local environment. The
[automation guide](automation-workflow.md#github-advisory-publication) describes
the credential checks performed before Cirra can publish.

## Review and publish from PowerShell

Run these commands from the repository root. Replace the example App and
installation IDs, key filename, PR number, policy path, and run ID with the
operator's actual values. Use a trusted local policy that allowlists the
repository and selects the reviewer and expected CI checks.

```powershell
$appId = 123456
$installationId = 12345678
$keyPath = '.cirra/credentials/publisher.private-key.pem'
$policyPath = '.cirra/policy.yaml'
$statePath = '.cirra/github-review'

& ./.venv/Scripts/cirra.exe review --provider github --repo owner/repository `
    --change 123 --config $policyPath --state-dir $statePath
```

Use the reported run ID for publication. This public-repository example uses
anonymous reads; an authenticated reader uses a separate credential selected
with `--token-env` for review and `--reader-token-env` for publication. Existing
`.env` files are not loaded automatically.

Store the PEM as a BWS secret. Map it to `CIRRA_GITHUB_APP_PRIVATE_KEY_PEM` in a separate
ignored GLAS publication manifest, then publish through GLAS:

```powershell
$env:GLAS_MANIFEST = 'config/glas.publisher.local.json'
& ./.venv/Scripts/python.exe scripts/with-glas.py publish --run '<run-id>' `
    --config $policyPath --state-dir $statePath --publisher-id $appId `
    --installation-id $installationId --app-key-env CIRRA_GITHUB_APP_PRIVATE_KEY_PEM
```

GitHub receives a neutral check with the exact examined revisions, findings,
coverage, and CI limitations. Keep `cirra/review` out of required merge checks.

### Require host builds and code review

On a GitHub worker route with the isolated observer and automatic Claude review
enabled, the operator can add this setting under `reviewer` in the trusted policy.

```yaml
merge_gate:
  branch: main
  block_at: high
  max_intermediate_comparisons: 32
```

Include the project build and test commands in the host observer policy and pin
its definition digest in the route's expected `cirra/isolated-tests` check.
For the selected target branch, the worker opens `cirra/code-review` while the
run is in progress. It publishes success after it verifies passing exact-head CI,
complete patch coverage, a completed Claude review and zero findings at or above
the threshold. A review limitation or failed run prevents a passing result.
The check summary identifies the PR head, target and local review report.

In the GitHub ruleset for that branch, require a pull request, an up-to-date
head, and both `cirra/isolated-tests` and `cirra/code-review` from the worker's
GitHub App. Leave the bypass list empty to enforce the same checks for admins.
GitHub associates checks with a commit. The worker issues its required review
on the PR head for the configured target branch. It verifies the ordered parents
of GitHub's current test merge immediately before publication. The reviewer
also receives any distinct patches that would appear if the target advanced to
an intermediate commit already in the PR head. Together with strict status
checks and blocked force pushes, this keeps the result valid when PRs are stacked.
Each collection can request up to 32 intermediate comparisons by default. The
operator can set this limit from 0 to 100. Snapshot retries share the allowance,
including comparisons that return identical patches. If the remaining allowance
cannot cover every possible base, the required review reports incomplete coverage
and prevents the merge. A smaller PR can bring the history within the limit.
Native CI success without a trusted checkout witness remains unverified in
Cirra's report; publishing does not upgrade that evidence.

## Confirm publication and recovery

Keep the same state directory and policy when publishing again. Repeating
publication updates the same owned check for that PR head. Adding `--reconcile`
performs reads and records a uniquely matching existing check without writing
another one or invoking the reviewer.

If a write's acknowledgement is lost, the local receipt remains `uncertain`.
Use `--reconcile` first. An absent or ambiguous result does not authorize an
automatic replacement. Inspect GitHub and the receipt before explicitly using
`--retry-uncertain`; a replacement create can produce a duplicate.

If the head or base changes, review the current snapshot before publishing.
A fresh review after a base-only change updates the same check on that head.
A new head receives its own check; superseded attempts cannot overwrite the
newer report. Publication always refreshes CI without purchasing another review.

See [GitHub's registration guide](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
and [private-key instructions](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)
for the account setup screens.
