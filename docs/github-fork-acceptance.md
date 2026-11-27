# GitHub fork acceptance

The controlled fork is `yizzity/cirra`, owned by Matt's second GitHub account. Its upstream is `mdn87/cirra`. On November 27, 2026, the account accepted collaborator access and created the fork from upstream commit `86b29876bc33c92d7ed5f4f1caeeb735e64ba800`.

## Live acceptance case

A documentation-only pull request from this fork exercises the existing production webhook, isolated checkout observer, subscription reviewer and GitHub App publisher. The source repository differs from the configured publication destination.

Completion requires observed evidence that:

- GitHub identifies the pull request head as `yizzity/cirra` and the base as `mdn87/cirra`.
- Cirra resolves the target branch in the base repository and binds the review to the exact fork head.
- The existing isolated test and review checks are published against that head in the base repository.
- Normal CI and required checks pass before the change is merged.

The fork and its collaborator access are verified. The live pull-request results will be recorded after execution; creating a fork alone does not complete the acceptance case.
