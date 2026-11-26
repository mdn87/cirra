"""File-backed queue compatibility check. Also runs on the host with stdlib Python."""

import json
import sys
from pathlib import Path

FORMAT = 2
MARKER = "worker-queue-format.json"


def live_format(root):
    path = Path(root) / MARKER
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))["format"]
        if type(value) is not int or value < 1:
            raise ValueError("invalid live worker queue format")
        return value
    return 0


def activate(root, minimum=1):
    from cirra.domain import CirraError
    from cirra.publish import write_receipt

    floor = live_format(root)
    if floor > FORMAT:
        raise CirraError("live worker queue requires a newer release")
    if floor < minimum:
        write_receipt(Path(root) / MARKER, {"format": minimum}, history=False)


def check(root, release):
    manifest = Path(release) / "worker-state-version"
    supported = int(manifest.read_text().strip()) if manifest.exists() else 0
    required = live_format(root)
    if supported == 0 and not required:
        # Recognize an interim installation that wrote the new files before a
        # global marker existed. Old queues already used audit-queue/archive.
        if any(
            (root / name).exists()
            for name in (
                "queue/archive",
                "queue/completed",
                "audit-queue/latest",
                "queue-slots.json",
            )
        ):
            required = 1
        else:
            for path in (root / "audit-queue").glob("*.json"):
                if (
                    json.loads(path.read_text(encoding="utf-8")).get("state")
                    == "awaiting_publication"
                ):
                    required = 1
                    break
            if not required:
                for path in (root / "audit-publication").glob("*.json"):
                    receipt = json.loads(path.read_text(encoding="utf-8"))
                    if receipt.get("uncertain") or receipt.get("state") in ("creating", "retry"):
                        required = 1
                        break
    if supported < required:
        raise ValueError(f"live worker queue format {required} exceeds release format {supported}")


if __name__ == "__main__":
    try:
        check(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        sys.exit(f"Worker queue compatibility check refused startup: {exc}")
