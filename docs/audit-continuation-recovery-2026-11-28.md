# Historical audit continuation, November 28, 2026

All twelve requested historical audits are complete. At 03:22 UTC, the final
readback verified all stored reports against authoritative production state
and all eleven native GitHub checks. The full PR 61 report remains scoped to
that change and did not replace the repository baseline.

The final two requested audits were admitted on the Intel Mac after the UTC
daily limit reset. The Mac was the authoritative data host. The scheduled
Linux continuation refused its standby copy, as its ownership check requires.
Matt's subsequent Mac credential verification was received and used; another
approval was not needed.

## Why the continuation did not finish

The schedule assumed Linux would own production when it ran. Its verification
command also checked only reports that were already complete, allowing an
exit status of zero while two requests remained unfinished. The operator helper
now requires all twelve frozen requests to be complete and rereads every stored
result and corresponding provider receipt. A negative check against the two
unfinished requests returned failure as required.

The full audit's initial structured-response runner failed before producing
accepted domain checkpoints. The replacement used the existing native session
reviewer on the approved data host. Each domain retained its exact historical
revision, catalog, source review and native checkpoint validation. Targeted
corrections preserved accepted domains; all ten domains eventually passed.

## Accepted reports

| Scope | Immutable revision | Coverage | Findings |
| --- | --- | --- | --- |
| PR 66 | `ca38cc4a66f49a0755a08206adeb060ba486ed5f` | 7 changed controls | None; all seven preserve uncertainty about external operational evidence. |
| Full audit scoped to PR 61 | `612dd54cd05678b412954dfe0b574861f00205d4` | 117 controls across 10 domains | 15 medium and 1 low. |

These reports assess historical source. Acceptance establishes that the report
is supported by its evidence; it does not mean its findings have been fixed or
that unobserved deployment behavior has been tested. Detailed reports remain
on the approved data hosts.

## Import under production ownership

The ordinary `cirra import-audit` command correctly refuses standalone writes
to enrolled state. The new [coordinated import operator](../scripts/import-coordinated-audit.py)
uses the existing worker lease and data-authority checks. It requires an exact
accepted report digest, validates an immutable temporary copy, holds the local
queue locks, refuses a changed state selector or standby, imports through the
native validator and store, and checkpoints authority before returning a receipt.
It does not publish or promote a restored host.

Workers are paused while the operator borrows their lease; webhook receivers
and voters keep running. The authoritative worker resumes first and completes
normal publication. A report import is not a completed job until the queue and
provider readback agree. See the [operator procedure](audit-workflow.md).

The import tests exercise replay, changed reports, an occupied lease, local
worker locking, standby and frozen-state refusal, authority changing during
admission, lease loss, and replacement of the original file during import.

## Completion evidence

- PR 66 published successful native check
  [140848067185](https://github.com/mdn87/cirra/runs/140848067185), with stored
  report digest `d0f4224b3b3cf6bbe0a4955699b098615d7e5e0033144aab4b42cd8c2547b4f0`.
- Full PR 61 attempt `4b1c0f6be5974e1b93e269b8af0f6908` completed with 117 rows
  and digest `b198cb134b9aca405b1510e91a275c5b78331ecff4ac90ee2b3b100eb7569168`.
  Its native publication state is `not_published`, as required for this scope.
- Repository baseline `ea6be755fe5f48edbe5a2f64856b45df` was unchanged before
  and after both imports. No audit admission was added or quota reset.
- The latest frozen Mac snapshot was independently restored before Linux
  resumed authority. Both receivers returned healthy responses during transfer.
- The nine new import tests and eighteen existing authority/import tests passed.
  Twelve operator-helper checks passed, including refusal to report completion
  with unfinished requests. The full local suite passed: 582 tests, with 60
  platform or integration skips. Repository lint and formatting passed.

Sanitized receipts are in
`.lugos-audit/mobile-rollout-20261126/audit-closeout-20261128/`. The authoritative
private reports and per-domain source acceptance records remain on lugos-host.
