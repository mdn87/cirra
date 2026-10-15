# Collect GitHub changes and CI evidence

`cirra collect` makes read-only requests to `api.github.com` and saves a local
snapshot for the existing session review workflow. It neither checks out nor
executes PR code, starts an agent, or publishes a report.

```text
cirra collect --provider github --repo mdn87/cirra --change 2 --config examples/github-policy.yaml
cirra prepare-review --snapshot <snapshot-path> --config examples/github-policy.yaml
cirra import-review --snapshot <snapshot-path> --config examples/github-policy.yaml --run <run-id> --response response.json
```

Use the same `--state-dir` throughout if overriding `.cirra`. Collection returns
`snapshot_path`, file/check counts, coverage gaps, and check collection errors.
It saves captures as JSON under `<state-dir>/captures/`. A successful capture
returns 0, a saved partial capture returns 1, and invalid input or a fatal
collection failure returns 2. Exit 0 means collection succeeded, not that CI
tested the right revision or that the change is safe. `--snapshot` is an alias
for the existing `--fixture` input option.

The example policy allowlists Cirra's stable GitHub repository ID and four native
CI job names from the GitHub Actions app. For another repository, explicitly
configure its numeric repository ID, expected check app IDs/names or commit
status creator IDs/contexts. A matching display name alone cannot satisfy policy.

## Credentials and limits

Public repository reads need no authentication. For private reads or a larger
rate allowance, explicitly select a reader token from the process environment:

```text
cirra collect --provider github --repo OWNER/REPO --change 123 --config policy.yaml --token-env CIRRA_GITHUB_TOKEN
```

Provision a token limited to the selected repository with Metadata, Contents,
Pull requests, Checks, and Commit statuses read permissions. Cirra issues only
GET requests; it does not independently certify the token's granted permissions.
There is no implicit use of `GITHUB_TOKEN`, GitHub CLI authentication, or `.env`.
Tokens remain in the transport and are absent from captures and review packets.
The collector refuses redirects and never follows URLs found in PR data,
check links, or pagination headers. Only GitHub.com is supported initially.

Collection uses an overall time budget (`--timeout`, default 60 seconds),
at most three attempts for transient network/5xx failures, and a 10-second
socket timeout. The overall budget is checked between reads/requests; one
in-flight socket read can overrun that budget by its timeout. Authentication
and rate-limit errors do not trigger automatic retries. Each API response is
capped at 8 MiB; exported snapshots are capped at 16 MiB. Reviewer input/output
limits remain those in operator policy.

## Snapshot and evidence rules

The collector reads PR metadata, compares the pinned target and head commits,
records the returned merge base, collects native checks/statuses, then reads
PR metadata again. A changed head, target, or file count retries the snapshot,
up to three attempts within the same request budget. Continual changes fail
without exporting a mixed snapshot. Fork heads are compared within the base
repository's API; unsupported comparisons produce a visible collection error.

The [comparison API](https://docs.github.com/en/rest/commits/commits#compare-two-commits)
returns at most 300 changed files. Cirra compares the returned count with PR
metadata and records omitted coverage. Missing/binary patches, malformed hunks,
diff budget exclusions, and addition/deletion count mismatches remain visible
gaps. It does not silently turn a truncated change into a complete review.

[Check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)
are fetched with `filter=all`; [commit statuses](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference)
are fetched separately. Both paginate, with `--max-pages` defaulting to 10 per
endpoint. Failed/incomplete pages, malformed records, duplicate native IDs,
and page-limit exhaustion become collection errors. Known earlier results stay
visible, but cannot make an incomplete collection look complete. Raw native
states are retained alongside normalized states; unknown states remain unknown.
Missing run IDs, attempt numbers, and start times are null rather than invented.
Check-suite IDs are retained separately. Competing runs with missing ordering
information remain ambiguous.

**A native success is not proof of a checkout.** This adapter records check/commit
association only. It does not promote GitHub Actions results to `head_exact`
or `merge_exact`, and does not trust arbitrary checkout metadata artifacts.
With required CI, the report therefore remains incomplete even when all four
native jobs succeeded. Trusted checkout provenance is still an unfinished part
of milestone 2. Review-only policy remains explicit and does not verify CI.

To refresh, collect again and prepare with the new snapshot. Unchanged review
inputs reuse the attempt; CI observations and collection errors are not part
of the review identity. Changed head, target, merge base, patch, or policy
creates a distinct analysis. Existing capture files are historical evidence;
`show` and importing a response do not contact GitHub. Future publication must
recheck the current remote revision before any write.

Live acceptance so far: collection of Cirra PR #2 captured 13 files and four
native checks and produced a session packet. Automated tests cover head/base
races, paginated reruns, missing coverage, outages, credential-safe failures,
and reuse. The planned controlled live cases, checkout attestation, reviewer
evaluation, and advisory publisher are not yet complete.
