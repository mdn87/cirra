# Changelog

## 1.0.0 — release candidate

Cirra coordinates Git-provider evidence, isolated checkout tests, advisory
reviews, production audits and existing release workflows. Git providers
retain repository hosting, permissions, CI execution and merge enforcement.

- Capture GitHub, GitLab and Forgejo changes; run offline fixtures or import
  revision-bound session reviews. Native Claude subscription reviews run
  without tools or repository credentials in their child environment.
- Publish GitHub checks and GitLab notes with durable intents, native readback,
  signed execution evidence and stale-revision protection.
- Run pinned Linux containers with read-only inputs, disabled networking and
  enforced resource limits. Linux production requires a dedicated account
  and rootless engine.
- Coordinate one active publisher across Linux and Intel Mac using three
  quorum voters, encrypted recovery, managed rollout/rollback and monitoring.
  The accepted recovery-point objective is at most 24 hours within the
  documented availability boundary.
- Queue `pr`, `full` and `release` audits with admission budgets, immutable
  packets, validated imports and release gates. Audit-read HTTP access
  requires a bearer token.
- Distribute under MIT, including the attributed Lugos validator and catalog.
  Dependencies retain their own licenses.

Distribution is planned through GitHub Release with wheel, source archive
and `SHA256SUMS`, without PyPI. See [the release decisions](docs/release-acceptance.md)
for availability limits and publication prerequisites. This candidate does
not waive release gates or certify unfinished audits.
