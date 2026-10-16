"""Real child processes for crash/concurrency tests, never imported by production code."""

import json
import os
import sys
import time
from pathlib import Path

from cirra.config import load_config
from cirra.coordinator import review
from cirra.fixture import FixtureProvider
from cirra.store import Store


def main():
    mode, config_path, fixture_path, directory = sys.argv[1:5]
    directory = Path(directory)
    config = load_config(config_path)
    provider = FixtureProvider(fixture_path, config)
    store = Store(directory)

    def pause(*args, **kwargs):
        (directory / "paused").write_text("ready", encoding="utf-8")
        while True:
            time.sleep(0.1)

    stage = {
        "before-call": "begin_call",
        "after-call": "record_response",
        "after-response": "finish",
        "after-analysis": "save_report",
    }.get(mode)
    if stage:
        setattr(store, stage, pause)

    class CountingReviewer:
        def review(self, packet):
            with (directory / "calls").open("ab", buffering=0) as calls:
                calls.write(b"call\n")
                os.fsync(calls.fileno())
            # Still outlive the initial lease, so a missing heartbeat fails.
            time.sleep(config.claim_ttl_seconds + 1)
            return provider.response

    result, reused = review(
        provider,
        config,
        store,
        reviewer=CountingReviewer(),
        rereview="--rereview" in sys.argv,
    )
    print(json.dumps({"report": result, "reused": reused}))


if __name__ == "__main__":
    main()
