# Cirra original concept

Recorded on 2026-10-15 from the founding conversation supplied by Matt. The
architecture, milestones, and technology choices below capture the original
proposal. Provider comparisons and naming concerns reflect that discussion and
remain research topics for implementation.

## Where the idea began

Matt asked:

> What kind of github alternatives? Doing things like GitHub actions and ci reviews have got me thinking

The discussion separated three systems that GitHub brings together:

1. **Git hosting and collaboration:** repositories, branches, pull requests,
   issues, and code reviews.
2. **CI/CD automation:** builds, tests, scheduled jobs, deployments, and other
   workflows.
3. **Automated code review:** linters, security scanners, and AI agents examining
   changes before merging.

The central opportunity was to separate the repository, automation engine, and
AI reviewer so each could evolve independently. Workloads could run locally or
in the cloud, and reviewers could use different models without tying the whole
development process to one provider.

Matt then proposed making it a project:

> should we build a repo for this? (ironic huh?) ... we could name it cirra

The founding idea embraced using GitHub to develop a system that could reduce
dependence on GitHub's ecosystem.

## Name and mission

**Cirra: Continuous Integration, Review, and Release Automation.**

**Mission:** Build a portable, AI-assisted development automation system that can
operate across Git providers, execution environments, and AI models.

Cirra would provide an independent coordination layer over existing platforms.
Git hosting and collaboration would remain with providers such as GitHub,
Forgejo, and GitLab. Cirra would coordinate workflows, execution, review,
evaluation, and policy across them.

The project would have a broader purpose than comparing hosting alternatives:
use real development workflows to evaluate and improve automation continuously.

## Alternatives that prompted the concept

The initial discussion identified the following candidates and reasons to
investigate them:

| Candidate | Initial reason to investigate | Hosting option discussed |
| --- | --- | --- |
| GitLab | An all-in-one platform with CI/CD, reviews, and security tooling | Self-hosted or hosted |
| Forgejo | An independent, lightweight Git platform with Actions | Self-hosted |
| Gitea | A lightweight GitHub-like platform emphasizing Actions compatibility | Self-hosted |
| Codeberg | A nonprofit-hosted Git service based on Forgejo | Hosted service |
| Azure DevOps | Pipelines, testing, and enterprise workflows | Cloud and Server editions |
| Bitbucket | Git hosting, Pipelines, and Atlassian integration | Cloud and Data Center editions |

GitLab was the initial all-in-one candidate for automation and formal review;
the discussion flagged paid tiers for some advanced approval controls. Forgejo
and Gitea were especially interesting for a home server. Forgejo's workflow
concepts were described as similar to GitHub Actions, with compatibility
differences to investigate before attempting workflow portability.

The discussion also considered replacing only GitHub's automation layer:

- **Self-hosted GitHub Actions runners:** retain GitHub repositories, pull
  requests, and workflows while executing jobs on owned hardware. The original
  discussion described self-hosted runner minutes as uncharged; pricing would
  need checking when choosing an execution model.
- **Woodpecker CI:** investigate an independent CI/CD system for Docker-based
  jobs and integration with GitHub, GitLab, Forgejo, and Gitea.
- **AI review tools:** investigate open-source PR-Agent, commercial reviewers,
  and GitLab's agentic review capabilities alongside conventional CI checks.

## First deployment approach

Keep existing repositories on GitHub while experimenting with Forgejo on an
available server. Use repository mirroring to compare workflows, pull-request
reviews, and integrations without committing to a full migration.

The initial hybrid arrangement was:

| Component | Proposed responsibility |
| --- | --- |
| GitHub | Primary repositories and collaboration |
| Local CI runner | Builds, tests, and scheduled jobs |
| AI review agent | Pull-request analysis and suggested improvements |
| Review and merge gates | Automated checks plus human approval where needed |

This approach would make it possible to evaluate the execution and review
layers before changing the primary Git provider.

## Proposed architecture

GitHub, Forgejo, GitLab, and local Git would connect to **Cirra's workflow
orchestration and policy engine**. That engine would coordinate:

| Area | Proposed responsibility |
| --- | --- |
| CI pipelines | Builds, tests, and validation |
| AI reviewers | Multiple models and agents |
| Execution | Cloud, local, and isolated runners |
| Evaluation | Review quality, speed, and cost |

Provider adapters, execution choices, and AI models should be replaceable as
the project learns which combinations work well.

## Initial development milestones

1. **Research and comparison.** Evaluate GitHub Actions, Forgejo, GitLab,
   self-hosted runners, and AI review systems.
2. **Working proof of concept.** Trigger builds, tests, and automated AI reviews
   from pull requests.
3. **Review quality experiments.** Compare agents and models against the same
   changes. Measure false positives, useful findings, time, and cost.
4. **Provider portability.** Run equivalent workflows on GitHub and Forgejo,
   documenting differences rather than assuming complete workflow compatibility.
5. **Self-hosted execution.** Introduce disposable, isolated runners on owned
   hardware with restricted credentials and permissions.

## Suggested starting stack

- **Python** for the initial orchestration and evaluation tools.
- **YAML** for declarative workflows.
- **Docker** for isolated execution.
- A frontend only when the experiments reveal a concrete need for a dashboard.

These were starting suggestions; the founding conversation did not implement
or validate a stack.

## First proof of concept

The first experiment should be deliberately narrow:

1. Open a test pull request.
2. Run conventional checks.
3. Obtain an AI review.
4. Publish the findings for review.

The AI reviewer would have no merge privileges. Conventional tests and AI
findings would remain distinct inputs to the review process.

The original runner security requirement was to isolate untrusted pull-request
code from personal files, credentials, and the normal development environment.
Disposable containers or VMs with restricted permissions were proposed as the
execution boundary.

## Repository and naming considerations

The original proposal was to start with a private `cirra` repository under
`mdn87`, keep the architecture and evaluation plan in `docs/`, and use Cirra's
own pull requests as its first test cases. This records the initial visibility
suggestion; it does not establish the repository's current visibility.

The conversation identified **Cirra AI** and **Cirra.io** as existing uses of the
name and flagged naming research if Cirra becomes a public developer tool.

The guiding principle was that Cirra should continuously evaluate and improve
its own development workflows, with its own changes providing practical test
cases for the automation it builds.
