# Production deployment, November 27, 2026

Cirra now accepts GitHub deliveries through `https://cirra-hooks.newman.foo/hooks/cirra-github` on either approved data host. The primary is publishing from the verified state returned by the Intel Mac. The Mac has installed the primary's latest verified recovery copy and is eligible to take over. Nodens supplies the third vote and encrypted backup storage. The original worker is stopped and fenced against automatic restart.

## Observed production checks

| Check | Observed result |
| --- | --- |
| Enrollment | Both roots matched the quiesced source before native enrollment; existing daily spending was preserved. |
| Primary role loss | With the primary worker, dedicated Cloudflare connector and voter stopped, the Mac acquired authority in 3.45 seconds. All 20 sampled public health requests returned HTTP 200. |
| Signed admission | The same genuine PR 69 delivery received HTTP 202 from each sole production origin. Replays retained job `7bff5e8c26fc5f1913a30f0f6379f695b72030ee33738d1233eda464b3af51bd` and returned `duplicate=true`. |
| Majority loss and recovery | Signed admission returned HTTP 503 with one voter, then HTTP 202 after the other voters recovered. |
| Native publication recovery | In the separate verification namespace, the primary submitted real GitHub check [140356241739](https://github.com/mdn87/cirra/runs/140356241739), discarded its acknowledgment, and the Mac recovered the exact check without another POST. This used the real provider and the native publisher. |
| Private backups | Primary capture at 08:04 UTC and Mac capture at 08:12 UTC both passed independent restore verification. The frozen Mac transfer at 08:14 UTC was restored on the primary before it resumed publishing. The returned primary's 08:21 UTC backup passed and was independently fetched and installed on the Mac. |
| Audit reader | The canonical read-only reader switched to the managed root. All 139 Taskmaster responses matched, including 55 attempts and 38 gate receipts; four complete sampled reads passed during the warm switch. |
| Provider delivery | GitHub delivery `3856383103226355712` at 08:32:00 UTC reached the new URL and received HTTP 202 with the same committed job and `duplicate=true`. |
| Handoff window | The complete retained delivery listing covered the final capture through cutover. Its only later delivery was the verified new-route redelivery; no accepted legacy deliveries remained unreconciled. |
| Monitoring | The primary health service reads the managed root and passed. Both receiver/worker pairs and scheduled recovery jobs are enabled; both Cloudflare connectors rejoined. |

These are observed results for the exercised failures. The Mac still requires FileVault unlock and its GUI login after a cold reboot. Loss of a voting majority deliberately prevents new admissions and publication until a majority recovers.

## Operational adjustments

The first coordinated replay matched a legacy PR 69 job that was already complete under the previous route policy. The operator verified the original and restored terminal records were identical and matched the signed payload, repository and change identity, then used native settlement to acknowledge that completed job. Its private record was preserved and the review was not rerun.

The Linux backup template uses a root-owned credential adapter at `/usr/local/libexec/cirra-ha/run-coordinated-backup.py`. It validates the exact systemd credential directory and root-owned files, copies credentials into mode-0600 private scratch for the native transfer CLI, and removes that scratch on exit. The long template instance uses the host-encrypted witness credential at `/etc/credstore.encrypted/cirra-backup-witness`; the original witness key was retained. Drop-ins `85-local-encrypted-witness.conf` and `90-private-file-contract.conf` preserve the scoped repository and approval bindings. The service still requires independent off-host restoration before the worker records a verified backup.

The webhook update initially used GitHub's general update endpoint, which reset the payload format and removed the signing secret. The existing key was restored directly from the approved primary webhook role without exporting it, JSON formatting and TLS verification were restored, and GitHub's fresh signed delivery passed. No intervening delivery appears in the retained delivery window. Future URL-only edits must use the [dedicated webhook configuration endpoint](https://docs.github.com/en/rest/repos/webhooks#update-a-webhook-configuration-for-a-repository).

The previous Tailscale URL remains usable and now proxies to the coordinated receiver on port 18796. All other Tailscale handlers, including `/weir-field/`, were unchanged. Preserve the enrollment fences; returning to the old standalone worker requires reconciling subsequent state and quorum history.

## Remaining acceptance work

- Historical PR audits 56–65 have complete stored reports and matching native GitHub receipts. PRs 61 and 62 were settled by source-informed lead adjudication with the pinned validator, immutable packet checks and import/provider readback. The helper's incomplete report envelope was corrected; no independent acceptance was fabricated.
- PR 66 and the full audit scoped to PR 61 remain unadmitted because the daily maximum of ten has been reached. Their audit policy still matches. The finite continuation is scheduled for **November 28 at 00:05 UTC** (November 27 at 7:05 p.m. Eastern), with verification at 01:05 UTC. The helper now follows the selected coordinated root and refuses a retired source or a primary without current authority. Completion still requires actual validated reports and final receipts. The full report retains the original repository baseline and requires 117 controls.
- The second-owner fork exercise still requires a fork of `mdn87/cirra` under another account Matt controls. The readback of `yizzity` found no Cirra repository or public fork. This acceptance case is independent of the completed production cutover.

Operational receipts are retained under `.lugos-audit/mobile-rollout-20261126/`, principally `production-control/`, `ingress/production-github-route.json`, `ingress/final-delivery-reconciliation.json`, and `reader/managed-reader-result.json`. Private reports and recovery archives remain on the approved data hosts. The ingress and promotion UTP grants were both recorded as `applied` with no mismatch.
