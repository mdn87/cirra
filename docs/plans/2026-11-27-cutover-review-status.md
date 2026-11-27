# Cutover review status

Checked the coordination-layer trail, production hosts, Cloudflare and GitHub on November 27, 2026.

| Review | Observed state | Next action |
|---|---|---|
| V-20261126-ceea5510 | Public webhook and promotion approvals are executing; both have verified answers. The reader grant is applied. | Inspect actual effects and ownership before continuing; reconcile if execution becomes uncertain. Do not request duplicate approval. |
| V-20261127-362d8b16 | Both connectors joined. Matt authorized publishing the hostname and Cloudflare confirmed it. The operator closed the superseded access request. | Exercise signed deliveries and native publication on the coordinated service before changing GitHub. |
| V-20261126-0688d8ca | Mac handoff R-20261126-58520c24 revision 1 is presented with no answer. | Matt enrolls the scoped Bitwarden account in local Keychain and permits the voter through the Mac firewall; then verify remotely. |
| V-20261126-1b0fe1c9 | Dedicated backup approval is applied. | Preserve the recorded restore evidence. |
| V-20261126-d1fe7174 | Tiered retention was selected. | Preserve preview-only scope; this does not authorize deletion. |
| V-20261126-ec3bf785 | Matt proposed creating yizzity. | Confirm a Matt-controlled second owner and actual fork before the live acceptance test. |

At 05:29 UTC, `cirra-hooks.newman.foo` returned HTTPS 200 from the Intel Mac while the Linux connector was stopped, then from Linux while the Mac connector was stopped. Each reply matched a unique value served by that host on a temporary test path. Both connectors rejoined with four edge connections each. The probes used the authoritative DNS answer while the homelab resolver still cached the earlier missing record.

GitHub webhook 698884839 still uses the original Linux receiver. Its latest recorded delivery returned HTTP 202. The coordinated receiver port is awaiting enrollment. The Intel Mac still needs its dedicated Bitwarden account in Keychain and permission for incoming voter connections. The private continuation runbook records the remaining production checks.

Of the twelve historical audit requests through PR 66, the worker verified native GitHub receipts for PRs 60, 64 and 65. Seven more PRs were admitted. PR 66 and the separate full audit of PR 61 still await admission.
