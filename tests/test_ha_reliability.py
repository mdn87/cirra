"""Regression contracts for lease deadlines and bounded subprocess cleanup."""

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from cirra import quorum
from cirra.dispatch import bounded_process
from cirra.domain import CirraError
from cirra.observer_process import run_control


class LeaseQuorum:
    def __init__(self):
        self.config = SimpleNamespace(lease_seconds=15)
        self.value = None
        self.failures = 0

    def get(self, key):
        return self.value

    def lease(self):
        if self.failures:
            self.failures -= 1
            raise quorum.QuorumUnavailable("transient renewal failure")
        return "1"

    def compare(self, checks, puts, *, leases=None, deletes=()):
        if checks.get(quorum.OWNER_KEY) != self.value:
            return False
        self.value = puts.get(quorum.OWNER_KEY, self.value)
        if quorum.OWNER_KEY in deletes:
            self.value = None
        return True


def test_owner_refuses_after_confirmed_deadline_even_if_local_timer_is_delayed(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(quorum.time, "monotonic", lambda: clock[0])
    voters = LeaseQuorum()
    owner = quorum.Owner(voters)
    voters.value = owner.token
    clock[0] = 116.0
    with pytest.raises(quorum.OwnershipLost):
        owner.compare({}, {})


def test_slow_renewal_does_not_extend_an_already_expired_local_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(quorum.time, "monotonic", lambda: clock[0])
    voters = LeaseQuorum()
    owner = quorum.Owner(voters)
    voters.value = owner.token

    def slow_lease():
        clock[0] = 116.0
        return "2"

    monkeypatch.setattr(voters, "lease", slow_lease)
    with pytest.raises(quorum.OwnershipLost):
        owner.renew()


def test_transient_renewal_failure_retries_inside_confirmed_lifetime():
    voters = LeaseQuorum()
    voters.config.lease_seconds = 0.9
    owner = quorum.Owner(voters)
    voters.value = owner.token
    voters.failures = 1
    with owner.maintaining():
        time.sleep(1.1)
        assert owner.compare({}, {})


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group ownership")
@pytest.mark.parametrize("runner", ["review", "control"])
def test_exited_parent_with_pipe_holding_child_is_rejected_and_killed(tmp_path, runner):
    pid_path = tmp_path / "grandchild.pid"
    code = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
    )
    argv = [sys.executable, "-c", code, str(pid_path)]
    started = time.monotonic()
    try:
        with pytest.raises(CirraError, match="bounded output|deadline"):
            if runner == "review":
                bounded_process(argv, "", tmp_path, 1, 1024)
            else:
                run_control(argv, tmp_path, timeout=1, maximum=1024)
        assert time.monotonic() - started < 6
        pid = int(pid_path.read_text())
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout.strip()
        assert not state or state.startswith("Z")
    finally:
        if pid_path.exists():
            import signal

            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
