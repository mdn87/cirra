# Review through an existing agent session

Cirra can prepare a bounded review packet for a live agent session and validate
the returned findings. The agent can be an existing Codex or Claude session, or
an operator-selected reviewer in Autowork. No model API client, new API key,
agent scheduler, or runtime dependency on those tools is required.

This first integration exports and imports local files. It does **not** attach
to a running session, invoke a CLI, resume a session, or monitor agent progress.
The operator or coordinating agent passes the packet and response between Cirra
and the reviewer. Automatic dispatch is a separate adapter to add later.

## Run a review

Use `examples/session-policy.yaml` to select `kind: session` and
`endpoint: session://manual`. The endpoint is a local handoff identifier, not a
network address. `model` records the operator's intended model or selection
label; the response separately records the session's reported model. Use
`not-exposed` when a session ID or model is unavailable; never invent one.

```text
cirra prepare-review --config examples/session-policy.yaml --fixture examples/change.json
```

The receipt contains `run_id`, `packet_path`, `review_outcome`, and report paths.
`awaiting_session` means no response has been imported and the report is
incomplete. Successful preparation returns exit code 0 even while waiting.
The packet contains the pinned snapshot, available patches, reviewer instructions,
response JSON schema, size/count limits, and a digest. It excludes conventional
CI observations, fixture answers, credentials, and publication destinations.
The fixture's `reviewer_response` is optional in session mode and ignored.

Pass the packet contents to the selected reviewer. Its instructions require
review of the supplied patch only and a JSON response, with findings grounded
in the captured paths, old/new sides, and hunk line numbers. The envelope is:

```json
{
  "schema_version": 1,
  "run_id": "COPY_FROM_PACKET",
  "packet_digest": "COPY_FROM_PACKET",
  "session": {
    "tool": "codex",
    "id": "not-exposed",
    "model": "not-exposed"
  },
  "review": {
    "findings": [],
    "notes": []
  }
}
```

The empty arrays illustrate the shape, not a suggested review result. Each
finding requires `path`, `side`, `start_line`, `end_line`, `severity`, `title`,
and `evidence`; the packet includes the full schema. Severity is one of `info`,
`low`, `medium`, `high`, or `critical`. Put unlocated observations and missing
context in `notes`. Do not use Markdown fences around the returned JSON.

Save the actual response and import it with the original inputs:

```text
cirra import-review --config examples/session-policy.yaml --fixture examples/change.json --run <run-id> --response response.json
cirra show --run <run-id>
```

Use the same `--state-dir` for every command if selecting a nondefault location.
The importer checks the run, packet digest, exact policy bytes, pinned head,
diff base, target, and captured diff. Findings undergo the same strict location,
count, size, and text validation as the deterministic reviewer. A malformed
review is saved as incomplete; it never becomes an apparent clean review.
An invalid envelope or mismatched identity returns exit code 2 without accepting
the response. A saved complete/incomplete report returns 0/1 respectively.

## Reuse and recovery

Preparing again returns the same packet and run. If a response already exists,
preparation reuses it and refreshes the supplied CI evidence. Check
`review_outcome` before asking an agent to review again. Cirra never dispatches
a session automatically, so it cannot prevent an operator submitting a packet
to the same or multiple agents twice.

The first accepted response is immutable for that attempt. Importing an
identical response again is safe; importing different findings or attribution
requires `prepare-review --rereview`. That creates a distinct attempt and
rejects subsequent imports into the superseded attempt for the same analysis.

Cirra saves the response before validating it. After an interruption, either
repeat import or run preparation with the original inputs to finish validation
and regenerate the report without asking the agent again. Source snapshots and
policy files must remain available; SQLite stores their identity, not a backup
of those source files. No database migration is introduced.

CI changes alone do not change the packet. Head, base, target, patch, policy,
or reviewer-protocol changes create a different analysis and reject responses
bound to another packet. This local adapter does not fetch the current remote
revision: supplying old inputs intentionally records a historical review.
Future publication must independently check the current provider revision.

## Trust and limitations

Session IDs and model labels are **operator-supplied attribution**, not proof
of who produced the response. Packet digests bind content and catch mismatches;
they are not signatures. The report makes these limits explicit. Session time,
usage, and cost are unknown, not zero. Findings remain advisory and grant no
merge or publication authority.

The prompt requests a data-only review. Prompt text does not enforce a sandbox
or remove tools from an existing agent session; this adapter cannot attest to
tool isolation or prior conversation context. The operator must choose the
session and its tool permissions appropriately. Cirra itself never executes
the captured change or reads `.env`; it exports only the selected snapshot.
Inspect that snapshot before sharing it with a model, especially for private
repositories. Credentials embedded in supplied patch text are not automatically
redacted. Do not copy subscription authentication into repository files or CI.

The supplied example and its CI provenance are synthetic. A live agent reviewing
it demonstrates the handoff, not live GitHub collection or reviewer accuracy.
The next integration collects real GitHub snapshots and conventional checks;
automatic agent invocation can then reuse this response contract.
