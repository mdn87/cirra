# Offline review workflow

Milestone 1 provides `cirra review --fixture ... --config ...` and `cirra show`.
It exercises the provider coordination contracts before any live provider or
billable reviewer is connected. It does not publish, run changed code, load
`.env`, or use GitHub/GitLab tokens. The files in `examples/` contain invented
identities and recorded synthetic inputs, not live API captures.

## Inputs and authority

Select operator policy explicitly with `--config`. Cirra does not discover
configuration from a checkout or from a change. The YAML loader rejects unsafe
tags, duplicate/unknown keys, invalid values, and policies over 64 KiB.
`examples/policy.yaml` shows the complete version 1 policy:

- `repositories` allowlists the provider, instance, and stable repository ID.
- `ci.mode: required` requires a nonempty `expected` list. Each expected check
  identifies its kind (`check_run` or `commit_status`), source ID, and name.
  Version 1 accepts only verified `success` as satisfying an expected check.
  An empty set requires explicit `ci.mode: review_only`.
- This workflow uses `kind: fake`, `endpoint: fake://local`, with a recorded
  model label. A separate [session workflow](session-workflow.md) supports
  `kind: session`, `endpoint: session://manual`; neither enables network calls.
- `limits` caps diff bytes, response bytes, and findings, and sets claim expiry
  and the bounded wait for another invocation. Oversized/unsupported patches
  are omitted whole and recorded as coverage gaps. Responses over the limit
  fail validation. These limits do not claim to be live token/spend controls.

Fixture JSON has `schema_version: 1`, `change`, `files`, `coverage_gaps`,
`checks`, and `reviewer_response`. `change` records provider/instance/repository/
change identity and full head, diff-base, and target Git object IDs. Each file
has a relative POSIX `path`, optional `old_path` (otherwise null), `status`, and
unified hunk `patch` (otherwise null). File headers are not part of the patch.
Binary files, unavailable patches, malformed hunks, and collection omissions
remain visible limitations. A local-Git test creates actual commits and captures
their diff to exercise the same input boundary.

The example check includes native run/check/attempt IDs, raw state, source ID,
association SHA, checkout provenance, trigger, URL, and observation/start times.
Fixture provenance is a **simulation of trusted evidence**, not an attestation:
setting `trusted: true` in arbitrary JSON proves nothing about a real CI run.
The future live adapter must establish provenance independently. It must map
native app/creator IDs and states into this contract; the fixture adapter does
not parse raw GitHub or GitLab API responses.

## Reports and reuse

Every report separates the snapshot, CI assessment, reviewer result, findings,
coverage, and errors. `head_exact` means a trusted checkout SHA equals the head;
`merge_exact` also requires both captured head and target as merge parents.
`associated_only` retains native results without satisfying CI policy;
`unrelated` cannot satisfy policy. The latest start/attempt for an allowlisted
source wins; conflicting latest observations remain unknown. Cirra excludes
its own `cirra/review` output from policy evidence.

The analysis identity contains snapshot, diff/input, exact policy bytes, fake
review protocol, model identity, and fake response digests. CI observations are
excluded so a repeated invocation can reuse analysis and refresh checks. A
different head, base, target, patch, policy, or fake response requires a new
analysis. Formatting a policy differently also changes its digest. The selected
policy path is recorded in the report. `prompt_digest` currently identifies the
fake response protocol; it is not evidence of a model prompt or a model call.

The fake response must contain only `findings` and `notes`. Findings require
`path`, `side` (`old`/`new`), positive `start_line`/`end_line`, `severity`,
`title`, and `evidence`. Lines must exist in captured hunks on the specified
side. For renames, `path` is the current path on either side; Cirra supplies
the captured `old_path`. Invalid findings invalidate the whole response rather
than silently dropping defects. Unlocated observations belong in `notes`.
Reviewer text is escaped in Markdown, including links, mentions, HTML, and
GitLab quick actions. JSON preserves the original validated strings as data;
downstream renderers must escape them for their own destination.

`review` writes a durable report in SQLite and exports JSON/Markdown to
`<state-dir>/reports/<run-id>/<report-digest>.*`. `show --run ID` reads the latest
saved report for that attempt without refreshing evidence or invoking review.
`show --run ID --export` can restore exports after an interrupted filesystem
write. Each refresh gets its own report digest and observation timestamp.

| Command result | Exit code | Meaning |
| --- | --- | --- |
| Review has complete evidence | 0 | Reviewer succeeded; required CI has verified success/failure evidence; supplied diff coverage is complete. Findings and failed CI are still possible. |
| Review has incomplete evidence | 1 | Report was saved, with coverage gaps, unavailable CI, or a reviewer failure/uncertain outcome. |
| Input, claim, or local I/O error | 2 | An actionable error is printed; the requested report may not have been produced. |
| Show succeeds | 0 | Saved report was displayed, regardless of its evidence status. |

All output is advisory. An exit code of zero is not an AI approval or a merge
gate. Model usage and cost are explicitly unknown/null in the report; the
offline fixture makes no billable call.

## Crash and concurrency behavior

Use one local state directory for an operator's invocations. SQLite transactions
serialize claims for each change. Claims have an owner, expiry, heartbeat, and
increasing generation. Every checkpoint/report write checks this claim inside
its transaction. An expired or superseded owner cannot write; releasing an old
claim cannot release its successor. Keep the SQLite database on a local disk;
separate stores and network filesystems are not supported coordination methods.

| Durable point before interruption | Next invocation |
| --- | --- |
| Prepared, before marking the call started | Resumes the same attempt and calls the reviewer. |
| Call marked started, response not persisted | Saves an uncertain report; makes no automatic second call. This includes the small window before sending the call. |
| Response persisted, validation unfinished | Validates the saved response without another call. |
| Analysis complete, report/export unfinished | Reuses the saved result and rebuilds the report/exports. |

After an uncertain call, inspect the report and explicitly pass `--rereview` to
create a new attempt. Ordinary retries retain the uncertain outcome. A timeout
or malformed response is also persisted and never silently retried. Resume with
the original fixture and policy: SQLite retains identity digests, responses,
results, and reports, but does not replace your source input files. Schema
version 1 is initialized only in an empty database; there are no migrations.

## Validation and next integration

Tests cover ten sequential replays, explicit re-review, independent concurrent
processes, heartbeat renewal, process termination at four durable boundaries,
expired-owner fencing, CI refresh and spoofed sources, revision/base changes,
missing/unknown checks, diff limits, malformed findings, safe text rendering,
and actual local Git patches. Native CI runs tests/lint on Windows/Linux with
Python 3.11/3.14 and builds a distribution. Its checkout metadata artifact
records the checked-out SHA and parents for future controlled integration cases;
the artifact alone is not a trusted provenance mechanism against a malicious
workflow or code running in that job.

The session handoff now supplies the first reviewer integration for milestone 2.
Next is GitHub collection, followed by advisory publication and GitLab portability.
Live collection needs the test repository and read-only GitHub App credentials;
review submission needs an operator-selected session and approved repository data
scope. Session usage/cost remain unknown until a dispatcher can measure them.
Direct model API integration is optional. Publisher permissions remain separate
and come later. Local `.env` files are ignored by Git; the local workflows never
read them.
