"""Crash-recoverable hot-file counts. Occupancy changes use the enqueue lock."""

import json
import os
import re
import uuid
from itertools import islice

from cirra.domain import CirraError
from cirra.publish import write_receipt
from cirra.queue_format import activate

BOOTSTRAP_LIMIT = 32


def hot_paths(directory):
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name.endswith(".json"):
                yield directory / entry.name


class Slots:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory.parent / "queue-slots.json"

    def snapshot(self):
        if not self.path.exists():
            return None
        try:
            with self.path.open("rb") as stream:
                data = json.loads(stream.read(1025))
            if (
                type(data["count"]) is not int
                or data["count"] < 0
                or data.get("operation") not in (None, "add", "remove")
                or (data.get("operation") and not re.fullmatch("[a-f0-9]{64}", data.get("id", "")))
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, RecursionError):
            raise CirraError(
                "queue capacity record is invalid; worker maintenance must recount"
            ) from None
        return data

    def version(self):
        try:
            with self.path.open("rb") as stream:
                return stream.read(1025)
        except FileNotFoundError:
            return None

    def count(self):
        saved = self.snapshot()
        if saved is None:
            # Warm up a large legacy queue in the worker, without blocking ingress.
            with os.scandir(self.directory) as entries:
                sample = list(islice(entries, BOOTSTRAP_LIMIT + 1))
            if len(sample) > BOOTSTRAP_LIMIT:
                return None
            return sum(entry.name.endswith(".json") for entry in sample)
        count = saved["count"]
        if operation := saved.get("operation"):
            exists = (self.directory / (saved["id"] + ".json")).exists()
            count += int(exists) if operation == "add" else -int(not exists)
        return count

    def record(self, count, *, operation=None, identifier=None):
        if count is None:
            raise CirraError("queue capacity is being measured; retry after worker maintenance")
        activate(self.directory.parent)
        state = {"count": count, "generation": uuid.uuid4().hex}
        if operation:
            state.update(operation=operation, id=identifier)
        write_receipt(self.path, state, history=False)
