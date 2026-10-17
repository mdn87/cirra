# Cirra

**Continuous Integration, Review, and Release Automation**

Cirra coordinates existing Git providers, CI evidence, and advisory code reviews.
Providers retain repository hosting, workflow execution, permissions, and merge
enforcement. Cirra's Python CLI validates this coordination flow using local
snapshots, a deterministic test reviewer, or findings from an existing agent session.

The project began with a question about alternatives to GitHub and grew into an
idea for an independent layer coordinating Git hosting, CI/CD, and automated
code review. GitHub can host Cirra while Cirra explores more portable development
workflows.

The [original concept](docs/original-concept.md) records the motivation,
architecture, research candidates, and initial milestones from the founding
conversation.

The [coordination layer implementation plan](docs/plans/2026-10-15-coordination-layer-plan.md)
turns that direction into staged work: a GitHub review-and-report prototype,
followed by a GitLab portability proof and further provider integrations.

The [production audit tiers plan](docs/plans/2026-10-16-production-audit-tiers-plan.md)
selects, queues and gates `pr`, `release` and `full` production audits from the
events Cirra already handles. The [audit guide](docs/audit-workflow.md) covers
the policy, the worker, agent runners, imports, publication and the release gate.

## Run the offline example

Python 3.11 or newer is required. From the repository root on Windows:

```powershell
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/cirra.exe review --config examples/policy.yaml --fixture examples/change.json
```

On Linux or macOS, use `python3 -m venv .venv` and the executables in `.venv/bin/`.
If using `uv`, `uv venv --python 3.11` and `uv pip install -e ".[dev]"` are equivalent.
Installation downloads Python dependencies; the installed fixture workflow makes
no network calls and requires no accounts or API keys.

The command returns a run ID and paths to JSON/Markdown reports under `.cirra/`.
Repeating it reuses the review and refreshes the fixture's CI evidence. Add
`--rereview` to request a distinct attempt. Inspect a saved report with:

```text
cirra show --run <run-id>
cirra show --run <run-id> --format json
```

Use the same `.cirra/` state directory for concurrent invocations; `--state-dir`
selects an explicit location. A complete report may contain defects or failed CI:
**complete describes available evidence, not approval to merge**.

## Use an existing agent session

Prepare a fixed packet for a live Codex, Claude, or other agent session:

```text
cirra prepare-review --config examples/session-policy.yaml --fixture examples/change.json
```

Give the exported packet to your session and save its JSON response. Import it
using the same policy, snapshot, and state directory:

```text
cirra import-review --config examples/session-policy.yaml --fixture examples/change.json --run <run-id> --response response.json
```

This is a local handoff: Cirra does not start an agent process or require a model
API key. The session uses its existing authentication. See the
[session workflow](docs/session-workflow.md) for the response format, revision
binding, recovery, and the limits of session attribution. The example still
uses a synthetic diff and CI evidence, even when an actual agent reviews it.

## Current scope

Cirra supports offline reviews, manual session handoffs, automatic tool-free
Claude subscription reviews, and GitHub/GitLab/Forgejo capture. Explicit
GitHub check and GitLab note publishing preserve intents, recovery receipts,
and stale-review protection. Authenticated webhook intake refreshes native
state; release requests dispatch existing GitHub workflows.

The [automation guide](docs/automation-workflow.md) covers commands, credentials,
checkout witnesses and recovery. The [frozen evaluation](eval/README.md) records
two real-reviewer runs: six of six seeded defects found and no clean-case
findings in each. The fake reviewer is only a deterministic test aid.

The [service guide](docs/service-workflow.md) describes the authenticated HTTP
receiver, durable background worker, and independent Docker checkout observer.
It includes explicit configuration, Linux service units, credential separation,
and the HTTPS webhook setup for reviews triggered by an open pull or merge request.
The isolated observer supports both GitHub and GitLab; GitLab test receipts stay
separate from native pipelines and are summarized in the maintained MR note.
Backup CI can run without the reviewer, recover missed PR/MR events by polling,
and accept manual `enqueue`, `sync`, and `requeue` commands. Signed test results
are retained before publication. See the service guide for the coverage limits
and the settings that enable this independently of GitHub Actions.

GitHub App publishing, recovery and revision-change handling have been exercised
on [PR #5](https://github.com/mdn87/cirra/pull/5). GitLab sandbox MR !1 demonstrated
automatic review, one maintained advisory note across head changes, and unchanged
native pipeline and merge requirements during publication. Its native pipelines
passed and the sandbox setup is merged.
GitLab [MR !2](https://gitlab.com/mdn87/cirra-sandbox/-/merge_requests/2) also
completed the live webhook/worker flow with isolated tests, verified checkout
evidence, successful review, and one maintained note. Repeat processing reused
the execution and review; the MR is merged and its main pipeline passed.
Hosted CI remains association-only until an independent checkout observer
provides trusted receipts. Live release dispatch still needs its separate setup.
See the [live acceptance results](docs/automation-workflow.md#live-acceptance-results).
GitKraken, Autowork and Roundtable remain optional development tools.

## Collect a real GitHub change

```text
cirra collect --provider github --repo mdn87/cirra --change 2 --config examples/github-policy.yaml
cirra prepare-review --snapshot <snapshot-path-from-collect> --config examples/github-policy.yaml
```

Public repositories work without a token. Collection records the pinned head,
comparison base, target, patches, check runs, and commit statuses. Native CI
success remains unverified until trusted checkout provenance is available.
The [GitHub workflow guide](docs/github-workflow.md) covers permissions, limits,
CI refresh, and importing the session response.

The [GitHub App setup guide](docs/github-app-setup.md) covers installing the
advisory publisher, loading its signing key, and recovering interrupted writes.

## Development

```text
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m build
```

Run these with the virtual environment's Python. Native GitHub Actions runs tests
on Windows and Linux with Python 3.11 and 3.14. Tests create disposable Git
repositories and kill their own child processes to verify restart behavior.
