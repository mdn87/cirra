# Cutover review status

This records the pre-cutover checks through 06:32 UTC on November 27, 2026. The [production deployment record](../production-rollout-2026-11-27.md) contains the completed rollout. The fork review row below was updated after its live acceptance passed later that morning.

| Review | Observed state | Next action |
|---|---|---|
| V-20261126-ceea5510 | Public webhook and promotion approvals are executing; both have verified answers. The reader grant is applied. | Inspect actual effects and ownership before continuing; reconcile if execution becomes uncertain. Do not request duplicate approval. |
| V-20261127-362d8b16 | Both connectors joined. Matt authorized publishing the hostname and Cloudflare confirmed it. The operator closed the superseded access request. | Exercise signed deliveries and native publication on the coordinated service before changing GitHub. |
| V-20261126-0688d8ca | The operator closed handoff R-20261126-58520c24 after the Mac credential and voter checks passed. | Preserve the credential and restore receipts. Both data hosts can now reach all three voters. |
| V-20261126-1b0fe1c9 | Dedicated backup approval is applied. | Preserve the recorded restore evidence. |
| V-20261126-d1fe7174 | Tiered retention was selected. | Preserve preview-only scope; this does not authorize deletion. |
| V-20261126-ec3bf785 | Matt confirmed the second account; `yizzity` accepted collaborator access and created `yizzity/cirra`. PR 71 passed all seven checks on its first revision. | Fork acceptance is complete; preserve the [provider evidence](../github-fork-acceptance.md). |

At 05:29 UTC, `cirra-hooks.newman.foo` returned HTTPS 200 from each host while the other connector was stopped. Both unique test responses matched, and both connectors rejoined with four edge connections each. At 05:55 UTC the same hostname returned the expected response through ordinary homelab DNS and public TLS.

Both data hosts reached all three voters at 05:56 UTC. The Mac verified its worker and webhook credentials at 06:11 UTC. Its independent backup reader restored the encrypted 22:10 recovery point, authenticated 254 current and 345 historical witness records, and passed SQLite integrity checks at 06:17 UTC. The six-hour restore check is active. At 06:31 UTC the two data hosts produced matching proofs for their routing key, witness key and intake policy.

GitHub webhook 698884839 still delivers to the original receiver. GitHub recorded HTTP 202 for its latest delivery. The coordinated service requires signed admission, native publication and recovery tests before switching the webhook.

As of 06:32 UTC, the original worker had verified native GitHub receipts for PRs 56, 57, 58, 59, 60, 63, 64 and 65. The admitted reviews of PRs 61 and 62 still required import. PR 66 and the separate full audit of PR 61 awaited the next daily admission window.
