"""Process-lifetime per-change locks on one local state filesystem."""

import os
import time
from contextlib import contextmanager

from cirra.domain import CirraError, digest


@contextmanager
def change_lock(directory, key, wait_seconds):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{digest(key)}.lock").open("a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise CirraError(
                        "another invocation owns this change; retry using the same state directory"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
