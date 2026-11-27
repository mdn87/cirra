# GitHub fork acceptance

The controlled fork is `yizzity/cirra`, owned by Matt's second GitHub account. Its upstream is `mdn87/cirra`. On November 27, 2026, the account accepted collaborator access and created the fork from upstream commit `86b29876bc33c92d7ed5f4f1caeeb735e64ba800`.

## Live acceptance case

A documentation-only [PR #71](https://github.com/mdn87/cirra/pull/71), authored by `yizzity`, exercised the existing production webhook, isolated checkout observer, subscription reviewer and GitHub App publisher. GitHub classified the author as `COLLABORATOR`; the source repository differed from the configured publication destination.

The first reviewed revision was `9397d4f88b8c27964ae4dd314428938530c7ff39`, targeting upstream `main` at `86b29876bc33c92d7ed5f4f1caeeb735e64ba800`.

| Observed check | Result |
| --- | --- |
| Repository identity | GitHub identified the head repository as `yizzity/cirra` and the base as `mdn87/cirra`. |
| Signed production delivery | Delivery `3856388881236623360` at 09:16:50 UTC received HTTP 202 from `cirra-hooks.newman.foo`; the response admitted job `769e14f433583904155137bc085e828dd14b6894386934daa302a7e686b006b0`. |
| Isolated checkout | [Check 140416966620](https://github.com/mdn87/cirra/runs/140416966620) passed for the exact fork head, using the existing execution definition `e47c7d21b8a5168a7309cd9b7c490ae0ddea24a78ecaf99badf2985da643179b`. |
| Review and publication | [Check 140417016147](https://github.com/mdn87/cirra/runs/140417016147) passed in the base repository. Its report named the exact head and target above, with verified CI evidence and test merge `aa79c8cc25625926adfdfb20af9da40950e66f67`. Both Cirra checks belonged to App `5339263`. |
| Conventional CI | [Run 45972388712](https://github.com/mdn87/cirra/actions/runs/45972388712) passed dependency audit and all four Linux/Windows Python 3.11/3.14 jobs. |

All seven checks passed on this revision. The review's low-severity observation that the initial document lacked execution evidence is addressed by the observed results above.

This completes the live fork case for a controlled collaborator. It does not change the existing policy that automatically ignores authors without write access. No application code, service configuration, credential scope or merge rule was changed for this exercise.

Public metadata receipts are retained in `.lugos-audit/mobile-rollout-20261126/fork/`. Private review state and signed checkout receipts remain on the approved production hosts.
