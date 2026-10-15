# Cirra planning decisions and review

Date: 2026-10-15. Subject: the
[coordination layer plan](2026-10-15-coordination-layer-plan.md).

## Scope and collaboration

Matt's newest direction was decisive: Cirra coordinates the capabilities of
existing GitHub/GitLab-style platforms. The original hosting-alternatives
discussion supplied context; it did not turn this into a replacement forge,
migration project, or new CI executor.

Roundtable session `cirra-coordination-plan-20261015` used `three-amigos`.
A Claude facilitator framed the task; a Claude product participant and Codex
implementation/testing participants each completed their contribution. After
four moves, the configured one-round limit produced `halted` at the start of
reconciliation. The parent synthesized the completed contributions; the
Roundtable session itself did not converge.

The raw session remains in the ignored local path
`.roundtable/sessions/cirra-coordination-plan-20261015.md`. Its SHA-256 is
`34f0f87200f5b8404f0051b1d947b28a7d9ad8f3b16aede1ea52cdaa2b83483f`.

## Decisions made from the contributions

| Question | Decision and reason |
| --- | --- |
| Observe or execute CI? | All three participants supported observing existing CI first. The explicit CLI suggested by engineering/testing gives a complete first workflow without a resident service. |
| Second provider? | All three recommended GitLab to challenge GitHub-specific assumptions. Forgejo remains a later option for the original self-hosting interest. |
| Report surface? | Product suggested a check plus a comment. The parent selected one GitHub check initially to reduce credentials, duplicate writes, and recovery cases. GitLab uses one maintained MR note because its status API can affect pipelines. |
| Snapshot identity? | Engineering/testing rejected a head-only analysis key. Include comparison base, target context, input/configuration digests and attempts; preserve the distinction between an associated commit and a verified tested revision. |
| Storage? | Retain SQLite checkpoints and small publication receipts. Native object lookup provides duplicate detection; durable state also prevents repeated model calls and records uncertain outcomes. |
| Reviewer quality? | Use the tester's ten-case prototype target, with the stronger held-out and matching requirements added during review. This measures a demonstration, not production accuracy. |
| Runtime tools? | Autowork/Roundtable were planning tools. The product remains independent of them unless a later adapter offers concrete value. |

## Autowork review and dispositions

Autowork ran one adversarial plan review through `claude-opus`:
`20261015-061406-cross-review`, status `succeeded`, with 17 findings. This status
means the review completed, not that the proposed product was implemented or
approved. The reviewed draft SHA-256 was
`5ee7b5fec2881888eb8db2c5f75e8293a90c82c2f9b3fb4d8f0fcfeb2d1cb027`;
the review manifest SHA-256 was
`b3ff5a5d4911c2a2ec86e86e258214aab27034709b9a0d4cca93bb97ab0d4dae`.

The parent checked provider-specific concerns against official documentation
linked in the plan and made these dispositions:

| Finding | Disposition in the revised plan |
| --- | --- |
| CR-01: tested revision provenance | Accepted the gap; defined exact, associated-only and unrelated relationships. Did not accept a trigger event alone as proof of actual checkout. |
| CR-02: publication identity | Defined a stable slot including provider instance, repository, change, head, publisher and report kind; re-review/base-only changes update that slot. |
| CR-03: base-only supersession | Require current head/base context and ordered publication generations; test a delayed B1 result after B2. |
| CR-04: publication freshness | Refresh CI without another model call; refuse a changed head/base. |
| CR-05: metadata/diff race | Compare pinned commits, record the merge base, re-read PR metadata and test snapshot conflict. |
| CR-06: delayed read visibility | Empty recovery queries remain uncertain. Use bounded reconciliation without blind creation; do not automatically choose among duplicate objects. |
| CR-07: uncertain-state recovery | Add reconcile and explicit uncertain-retry paths, with a recovery acceptance case and recorded duplicate risk. |
| CR-08: dead work claims | Add owner, expiry/heartbeat, fencing generation, takeover tests and explicit handling of a lost model-call outcome. |
| CR-09: unscheduled prerequisites | Milestone 1 adds normal repository CI; milestone 2 schedules test-repository, App, credential, model and limit setup. |
| CR-10: evaluation loopholes | Freeze finding-match rules, reserve one defective/one clean case, require the held-out defect in both runs and report spurious findings on defective cases. |
| CR-11: suppression injection | Add defects accompanied by instructions to suppress findings or claim approval; report those cases separately. |
| CR-12: removed/renamed lines | Add old/new diff side and old-path information; validate against the appropriate hunks. |
| CR-13: GitLab publication effects | Choose a maintained MR note; test unchanged pipelines and merge eligibility. The parent also added quick-action neutralization based on the Notes API. |
| CR-14: trusted evidence sources | Define app-plus-name and creator-plus-context identities; exclude unlisted sources. |
| CR-15: excess state machinery | Partially adopted: native find/update is primary. Retain small local intents/checkpoints for crash history, ownership and avoiding repeated review spend; no distributed queue. |
| CR-16: trusted baseline ambiguity | Use operator-local configuration only in the prototype and retain its source/digest. |
| CR-17: model data handling | Allowlist approved repositories and endpoints, record the endpoint, and require an explicit choice for private-repository data. |

The parent applied these corrections after the single review; no second model
review was run. Implementation acceptance checks in the plan are future work.
This documentation change is checked through link resolution, whitespace/full
diff inspection, and Lugos Audit. Local Autowork artifacts are excluded from Git.


## Implementation cross-review, 2026-10-15

Autowork run `20261015-224757-cross-review` completed successfully using its
Claude Opus route. One packet covered `src/cirra` and the two initial new test
files. Thirteen findings were adjudicated; the corrections are in PR #4.

- CR-01: Replaced database-wide transactions across network calls with
  process-lifetime per-change OS locks and short fenced checkpoints. An
  unrelated review now completes while a publisher is deliberately stalled.
- CR-02 and CR-08: Release requests require a merged candidate reachable from
  a protected target branch, use that branch's workflow definition, and require
  witness-verified native CI definition digests from operator policy.
- CR-03: Kept an older remote generation uncertain because an eventually
  consistent read cannot prove a PATCH failed. Explicit retry targets the known
  ID and records no duplicate-create risk. The message explains this recovery.
- CR-04: Local size refusals precede intent; definitive provider refusals become
  `failed`, while transport/malformed-acknowledgement cases stay `uncertain`.
- CR-05: GitLab slots are MR-scoped, maintaining one note across head changes.
- CR-06 and CR-11: An acknowledged release cannot be redispatched by the
  uncertain-retry flag. Stable request identity excludes observation timestamps.
- CR-07: Webhook deduplication also binds the authenticated payload digest;
  changing an unsigned delivery header does not cause another provider capture.
- CR-09 and CR-12: Missing acknowledged publications can be explicitly replaced
  after reconciliation/inspection. Canonical duplicate selection persists.
- CR-10: Publisher read/write response shapes are checked; malformed write
  acknowledgements remain uncertain rather than producing unsafe automatic retry.
- CR-13: Added regressions for these paths, foreign App ownership, restricted
  token grants and concurrent unrelated review work. Real local RSA JWT signing
  was also exercised without minting a remote credential.

The review's suggested automatic retry after an older PATCH read was not
adopted; it would infer non-delivery from visibility alone. No additional model
review was dispatched after the corrections. Credential-dependent live
acceptance remains explicitly open in the implementation plan.
