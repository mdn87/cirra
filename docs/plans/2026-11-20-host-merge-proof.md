# Host CI and code-review acceptance

On November 20, 2026, GitHub enforced both results from the Cirra GitHub App
on `main`. The rules require an up-to-date PR, block force pushes, and have
an empty bypass list. The operator set the blocking review threshold to high.
[Active ruleset](https://github.com/mdn87/cirra/rules/27713329)

| Pull request and head | Host build and tests | Host code review | Normal GitHub merge |
| --- | --- | --- | --- |
| #35 at `4cf1744`, immediately after opening | Missing | Missing | HTTP 405. GitHub expected both required checks. |
| #35 at `4cf1744`, after publication | Passed | Failed. GitHub supplied no patch for the empty evidence file. | HTTP 405. GitHub named the failing review check. |
| #36 at `aa7ac79` | Passed | Passed | GitHub merged the PR at `60023fc`. |

Both rejected attempts supplied the current PR head. The PR was open and
conflict-free, and `main` stayed at `e367e7d`. The paired check runs identify
the same head and App for each PR.
[PR #35 build](https://github.com/mdn87/cirra/runs/136193174384) ·
[PR #35 review](https://github.com/mdn87/cirra/runs/136204880047) ·
[PR #36 build](https://github.com/mdn87/cirra/runs/136204334811) ·
[PR #36 review](https://github.com/mdn87/cirra/runs/136205103668)

On `lugos-host`, the signed execution receipts matched the exact PR heads and
the pinned Docker definition. Ruff and pytest ran before the package build.
The verified container logs recorded a source distribution and a wheel. The
review reports identified Claude Sonnet 5.5 through the host CLI. The worker
verified the current test-merge parents and published the result on the PR head.
[Merged implementation](https://github.com/mdn87/cirra/pull/36)
