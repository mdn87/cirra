"""Process-lifetime per-change locks on one local state filesystem."""

import os
import threading
import time
from contextlib import contextmanager

from cirra.domain import CirraError, digest

_held = threading.local()


@contextmanager
def read_locked_file(path):
    """Use the owning handle for byte-range locks on Windows during quiesced backup."""
    stream = getattr(_held, "streams", {}).get(path.resolve()) if os.name == "nt" else None
    if stream is None:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as reader:
            yield reader
        return
    position = stream.tell()
    try:
        stream.seek(0)
        yield stream
    finally:
        stream.seek(position)


@contextmanager
def change_lock(directory, key, wait_seconds):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest(key)}.lock"
    with path.open("a+b") as stream:
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
            if os.name == "nt":
                if not hasattr(_held, "streams"):
                    _held.streams = {}
                _held.streams[path.resolve()] = stream
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                del _held.streams[path.resolve()]
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
