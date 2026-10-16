"""Bounded trusted Git/Docker subprocesses; source code only runs inside Docker."""

import os
import subprocess
import threading
import time

from cirra.dispatch import ENVIRONMENT, stop_process
from cirra.domain import CirraError


def control_environment():
    return {key: value for key, value in os.environ.items() if key.upper() in ENVIRONMENT}


def run_control(argv, directory, *, timeout=60, maximum=1024 * 1024, data=b"", env=None):
    process = subprocess.Popen(
        argv,
        cwd=directory,
        env=env if env is not None else control_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output, exceeded = bytearray(), threading.Event()

    def drain(stream, save):
        count = 0
        try:
            while chunk := stream.read(65536):
                count += len(chunk)
                if count > maximum:
                    exceeded.set()
                elif save:
                    output.extend(chunk)
        finally:
            stream.close()

    def send():
        try:
            process.stdin.write(data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, True), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, False), daemon=True),
        threading.Thread(target=send, daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if exceeded.wait(0.05):
                raise CirraError("observer subprocess exceeded its output limit")
            if time.monotonic() >= deadline:
                raise CirraError("observer subprocess exceeded its deadline")
        for thread in threads:
            thread.join(timeout=2)
        if exceeded.is_set() or any(thread.is_alive() for thread in threads):
            raise CirraError("observer subprocess did not return bounded output")
        if process.returncode:
            raise CirraError(
                "observer subprocess failed; inspect Git/Docker availability and policy"
            )
        return bytes(output)
    finally:
        if process.poll() is None:
            stop_process(process)
