"""Deterministic reviewer and strict validation of its untrusted response."""

import json

from cirra.domain import CirraError, fields, number, string

PROTOCOL = "cirra.fake.structured-findings.v1"


class FakeReviewer:
    def __init__(self, response):
        self.response = response

    def review(self, packet):
        # No imports, execution, tools, credentials, filesystem reads, or network from the packet.
        return self.response


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CirraError("duplicate response key")
        value[key] = item
    return value


def validate_response(response, snapshot, config):
    try:
        if len(response.encode()) > config.max_response_bytes:
            raise CirraError("reviewer response byte limit exceeded")
        data = json.loads(response, object_pairs_hook=unique_object)
        fields(data, ["findings", "notes"], label="reviewer response")
        if not isinstance(data["findings"], list) or not isinstance(data["notes"], list):
            raise CirraError("findings and notes must be lists")
        if len(data["findings"]) > config.max_findings or len(data["notes"]) > 100:
            raise CirraError("reviewer result count limit exceeded")
        files = {file.path: file for file in snapshot.files}
        findings = []
        for finding in data["findings"]:
            fields(
                finding,
                ["path", "side", "start_line", "end_line", "severity", "title", "evidence"],
                label="finding",
            )
            string(finding["path"], "finding path")
            string(finding["side"], "finding side")
            string(finding["severity"], "finding severity")
            file = files.get(finding["path"])
            if file is None or finding["side"] not in ("old", "new"):
                raise CirraError("finding does not identify a captured file and diff side")
            if finding["severity"] not in ("info", "low", "medium", "high", "critical"):
                raise CirraError("unsupported finding severity")
            start = number(finding["start_line"], "start_line", 1, 2**31 - 1, integral=True)
            end = number(finding["end_line"], "end_line", start, start + 1000, integral=True)
            lines = file.old_lines if finding["side"] == "old" else file.new_lines
            if not all(line in lines for line in range(start, end + 1)):
                raise CirraError("finding line range is outside the captured diff side")
            string(finding["title"], "finding title", 300)
            string(finding["evidence"], "finding evidence", 4000)
            findings.append({**finding, "old_path": file.old_path, "reviewer": config.model})
        notes = [string(note, "review note", 4000) for note in data["notes"]]
        return {"outcome": "succeeded", "findings": findings, "notes": notes, "errors": []}
    except (CirraError, ValueError, TypeError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, CirraError) else "reviewer response is not valid JSON"
        return {"outcome": "malformed", "findings": [], "notes": [], "errors": [reason]}
