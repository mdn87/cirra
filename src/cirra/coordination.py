"""Identify private state bound to a quorum deployment."""

from pathlib import Path

from cirra.queue_format import live_format

MARKER = "coordination-state.json"
INTENT = "coordination-enrollment.json"


def shared_state(directory):
    root = Path(directory)
    return (root / MARKER).exists() or (root / INTENT).exists() or live_format(root) >= 2
