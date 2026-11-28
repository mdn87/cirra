# v1.0.0 operator decisions

Matt answered the protected release review on November 28, 2026 at 16:05 UTC.
These decisions authorize the work below; they do not certify an unfinished
candidate or substitute for its tests and audits.

## License and publication

The operator approved the proposed MIT license, including the copied Lugos
validator and catalog, and GitHub publication of v1.0.0 after the release gates
pass. Publication includes the annotated tag, wheel, source distribution,
checksums and verified release notes. PyPI publication, data deletion, quota
resets and audit waivers are outside this authorization.

## Availability

The accepted deployment boundary is conditional single-site failover between
`lugos-host` and `intelmac`, with Nodens as the third voter and backup appliance.
Automatic promotion requires a voting majority, working credentials and an
eligible private recovery capture no more than 24 hours old. The recovery-point
objective permits at most one day of lost work within that covered scenario.
Without eligible recovery evidence, promotion must refuse to proceed.

The November 27 warm role-withdrawal exercise measured a 3.45-second takeover
and 20 successful public health samples. That is a measurement of the tested
failure, not a universal recovery-time or continuous-uptime guarantee. It was
not a physical power-loss test. See the
[November 27 evidence](production-rollout-2026-11-27.md).

The Intel Mac requires manual FileVault unlock and GUI login after a cold
reboot. Losing the voting majority refuses admission and publication. Losing
home internet or Cloudflare can make the service unreachable. These outage
limits are explicitly accepted for v1.0.0. No additional private-state host is
authorized by this decision.

## Linux isolation required before publication

The operator chose to require stronger Linux Docker isolation before release.
The current worker's rootful Docker capability is not an accepted v1.0.0 risk.
The candidate must establish a narrower privilege boundary and verify it with
native tests while preserving private-state recovery, authentication and the
existing execution limits. Publication remains dependent on that work.
