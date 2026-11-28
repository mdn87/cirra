"""Check, and optionally fix, each route's pinned observer definition digest.

The digest binds the observer image, commands, limits, launcher and supervisor
implementation, so any release that changes observer code changes it. Run this
with the new release's Python before restarting the worker; a stale pin makes
the worker refuse CI for that route.
"""

import json
from pathlib import Path

import yaml

from cirra.domain import CirraError
from cirra.observer import NAME, load_observer
from cirra.service_config import load_service, read_yaml


def upgrade_files(service_path):
    """Prepare an observer-only HA pin update; the deployment transaction writes it.

    Preserve the original policy bytes for admission identity. Execution keeps
    the new definition digest. No quorum keys, counters or credentials change.
    """
    service_path = Path(service_path).resolve()
    document, _ = read_yaml(service_path)
    config = load_service(service_path)
    files = {}
    for item in document["routes"]:
        route = config.routes[item["name"]]
        if not route.observer:
            continue
        current = load_observer(route.observer)["definition_digest"]
        pinned = [
            check["definition_digest"]
            for check in route.policy.expected_checks
            if check.get("name") == NAME
        ]
        if pinned == [current]:
            continue
        policy = Path(route.policy.path)
        raw = policy.read_bytes()
        if len(pinned) != 1 or raw.count(pinned[0].encode()) != 1:
            raise CirraError("observer upgrade requires one unambiguous policy pin")
        if "intake_policy" not in item:
            baseline = service_path.parent / ("intake-" + route.name + ".yaml")
            if baseline.exists() and baseline.read_bytes() != raw:
                raise CirraError("retained intake policy conflicts with this rollout")
            files[baseline] = raw
            item["intake_policy"] = baseline.name
        files[policy] = raw.replace(pinned[0].encode(), current.encode())
    if files:
        files[service_path] = yaml.safe_dump(document, sort_keys=False).encode()
    return files


def check_pins(service_path, *, repin=False):
    rows = []
    for name, route in load_service(service_path).routes.items():
        if not route.observer:
            continue
        current = load_observer(route.observer)["definition_digest"]
        pinned = [
            check["definition_digest"]
            for check in route.policy.expected_checks
            if check.get("name") == NAME
        ]
        row = {"route": name, "observer_digest": current, "pinned": pinned, "ok": current in pinned}
        if not row["ok"] and repin:
            policy = Path(route.policy.path)
            text = policy.read_text(encoding="utf-8")
            if len(pinned) != 1 or text.count(pinned[0]) != 1:
                raise CirraError(f"route {name}: cannot repin automatically; edit {policy.name}")
            policy.with_name(policy.name + ".before-repin").write_text(text, encoding="utf-8")
            policy.write_text(text.replace(pinned[0], current), encoding="utf-8")
            row.update(ok=True, repinned=True)
        rows.append(row)
    return rows


def main(service_path, repin):
    rows = check_pins(service_path, repin=repin)
    print(json.dumps(rows, sort_keys=True))
    return 0 if all(row["ok"] for row in rows) else 1
