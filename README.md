# Cirra

**Continuous Integration, Review, and Release Automation**

Cirra coordinates existing Git providers, CI evidence, and advisory code reviews.
Providers retain repository hosting, workflow execution, permissions, and merge
enforcement. Cirra's first implementation is an offline Python CLI that validates
this coordination flow using synthetic fixtures and a deterministic reviewer.

The project began with a question about alternatives to GitHub and grew into an
idea for an independent layer coordinating Git hosting, CI/CD, and automated
code review. GitHub can host Cirra while Cirra explores more portable development
workflows.

The [original concept](docs/original-concept.md) records the motivation,
architecture, research candidates, and initial milestones from the founding
conversation.

The [coordination layer implementation plan](docs/plans/2026-10-15-coordination-layer-plan.md)
turns that direction into staged work: a GitHub review-and-report prototype,
followed by a GitLab portability proof and further provider integrations.

## Run the offline example

Python 3.11 or newer is required. From the repository root on Windows:

```powershell
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/cirra.exe review --config examples/policy.yaml --fixture examples/change.json
```

On Linux or macOS, use `python3 -m venv .venv` and the executables in `.venv/bin/`.
If using `uv`, `uv venv --python 3.11` and `uv pip install -e ".[dev]"` are equivalent.
Installation downloads Python dependencies; the installed fixture workflow makes
no network calls and requires no accounts or API keys.

The command returns a run ID and paths to JSON/Markdown reports under `.cirra/`.
Repeating it reuses the review and refreshes the fixture's CI evidence. Add
`--rereview` to request a distinct attempt. Inspect a saved report with:

```text
cirra show --run <run-id>
cirra show --run <run-id> --format json
```

Use the same `.cirra/` state directory for concurrent invocations; `--state-dir`
selects an explicit location. A complete report may contain defects or failed CI:
**complete describes available evidence, not approval to merge**.

## Current scope

Milestone 1 implements local input validation, revision-aware CI assessment,
bounded review inputs, SQLite checkpoints and expiring work claims, crash recovery,
and JSON/Markdown reports. The example reviewer returns supplied fixture data;
it does not use an AI model or demonstrate review quality. GitHub collection,
real model review, native publication, and GitLab integration remain later
milestones. GitKraken, Autowork, and Roundtable are optional development tools.

See [the offline workflow](docs/offline-workflow.md) for schemas, exit codes,
recovery behavior, test coverage, and the next integration steps.

## Development

```text
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m build
```

Run these with the virtual environment's Python. Native GitHub Actions runs tests
on Windows and Linux with Python 3.11 and 3.14. Tests create disposable Git
repositories and kill their own child processes to verify restart behavior.
