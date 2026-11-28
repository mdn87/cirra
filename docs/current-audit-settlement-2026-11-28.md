# Current session audits, November 28, 2026

The four admitted audits for PRs 67, 68, 69 and 72 are complete. At 08:50 UTC,
readback matched each stored report to its accepted digest, original attempt,
immutable revision and native GitHub check. No admitted report remains waiting
for import on the authoritative Linux host.

## Results

| PR | Reviewed revision | Coverage | Findings | Native check |
| --- | --- | --- | --- | --- |
| 67 | `7cf79453ad10d833a8dd5ae35c6852f679dbf1e9` | No materially changed P controls | 0 | [140925612126](https://github.com/mdn87/cirra/runs/140925612126), success |
| 68 | `43b74638ceaa3fe765b7736ab1949123165800da` | No materially changed P controls | 0 | [140925617587](https://github.com/mdn87/cirra/runs/140925617587), success |
| 69 | `6bfe0eb2efcf01e12fcb46abbe0143e886aa37fb` | No materially changed P controls | 0 | [140925622810](https://github.com/mdn87/cirra/runs/140925622810), success |
| 72 | `0802f5b7aff0ded40196149056949286fc0a1bb9` | 4 changed controls | 2 | [140925628398](https://github.com/mdn87/cirra/runs/140925628398), success |

P controls are the production-audit catalog controls applicable to pull
requests. The three documentation changes record rollout status rather than
changing those controls. Their empty coverage is valid under the pinned PR
contract; they do not establish that historical deployment claims were retested.

PR 72's report passed after corrections to its locking and concurrency
analysis and an outdated note. Report acceptance establishes source support;
it does not mean its findings were fixed. Its two findings remain recorded in
the private report even though the configured native check concludes success.

## Verification and import

- Each report passed the pinned production-audit 1.1.0 validator and a separate
  source review. Reviewers received immutable changed files and, for PR 72,
  the relevant storage, authority, quorum and locking dependencies.
- The existing coordinated import operator checked the accepted digests,
  acquired the ordinary worker lease, imported through the native validator
  and store, and checkpointed authority. No admission or quota was reset.
- Both workers were paused for import while receivers and voters remained
  running. Linux resumed first, followed by the Intel Mac standby.
- All 20 public health samples returned HTTP 200 during the operation,
  from 08:49:31 through 08:49:50 UTC. Both local receivers were healthy;
  subsequent Linux worker readback reported no errors. The Mac resumed a
  fresh standby process and remained eligible for recovery.
- Repository baseline `ea6be755fe5f48edbe5a2f64856b45df` was unchanged.
  Both hosts still run release `5141d6ed2528914423dc024cfc53be5467de19af`.

Accepted report digests, in PR order:

```text
67  75407ac33d4881ed0290de362189f68979bda6d7db513a7f1d801afad4df36ce
68  a36689cac536c063049857b05899292c8a684fa35ff6c3de04df3f87634cd4bd
69  77ad4e37672cd2780f5a4915290214c1521bb3f62645bd2debb8f9a3a74d1060
72  4a11106ac2e55c36d8dfe75417076a14457ad08f388b4c0340f0cd3c1ec6346d
```

Sanitized receipts are in `.lugos-audit/current-audits-20261128/`. Detailed
reports, draft corrections and independent acceptance records remain on
lugos-host under the private audit directory's `current-audits-20261128/`.

## Remaining work

The 08:50 UTC authoritative census contains 31 completed jobs, 20 superseded
jobs, one queued repository full audit and three deferred PR audits (73–75).
Those four requests have not been admitted and remain subject to the daily
audit budget: November 28 usage is 10 of 10 admissions. They are separate from
this completed batch; the existing scheduler retries after the UTC date changes.

The reviewed operational follow-up still covers independent monitoring,
clock safeguards, local audit-read access and the legacy snapshot decision.
Retention deletion and audit-runner/Docker policy also remain unresolved.
The PR 72 findings must be included when reviewing the remaining concurrency
safeguards; this settlement does not close them.
