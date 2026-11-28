"""External release effects survive local rollback and lost acknowledgements."""

import copy
import json

import pytest
from test_audit_release import HTTP
from test_audit_release import candidate as candidate_fixture
from test_publication_fence import Cluster

from cirra import release
from cirra.domain import CirraError
from cirra.quorum import OwnershipLost, bind_owner
from cirra.store import Store
from cirra.transport import RemoteUncertain

release_candidate = candidate_fixture


class WorkflowHTTP(HTTP):
    def __init__(self, provider):
        super().__init__(provider)
        self.item = None
        self.hidden = self.lose_reply = False
        self.after_post = lambda: None
        self.trusted_sha = provider.snapshot.head_sha

    def request(self, method, route, params=None, body=None):
        if method == "POST":
            self.writes += 1
            self.item = {
                "id": 712,
                "display_title": "cirra-release-" + body["inputs"]["cirra_request_id"],
                "head_sha": self.provider.snapshot.head_sha,
                "head_branch": body["ref"],
                "event": "workflow_dispatch",
            }
            self.after_post()
            if self.lose_reply:
                raise RemoteUncertain("lost reply")
            return {}, False
        if "/actions/workflows/" in route:
            return {"workflow_runs": [] if self.hidden or self.item is None else [self.item]}, False
        if "/branches/" in route:
            return {"protected": True, "commit": {"sha": self.trusted_sha}}, False
        return super().request(method, route, params, body)


def test_enrolled_release_requires_quorum_owner_before_any_work(tmp_path):
    store = Store(tmp_path)
    (tmp_path / "worker-queue-format.json").write_text('{"format": 2}')
    with pytest.raises(CirraError, match="active quorum worker"):
        release.dispatch(None, store, None, None, None)


def test_restored_state_cannot_repeat_hidden_dispatch_and_recovers_visible_run(
    release_candidate, tmp_path
):
    provider, config, _, primary, report = release_candidate
    standby = Store(tmp_path / "standby")
    with primary.connection() as source, standby.connection() as destination:
        source.backup(destination)
    request = release.plan(report, 12, "main")
    http = WorkflowHTTP(provider)
    http.lose_reply = http.hidden = True
    cluster = Cluster()
    with bind_owner(cluster.elect()):
        assert (
            release.dispatch(request, primary, config, lambda: provider, http)["state"]
            == "uncertain"
        )
    assert http.writes == 1
    with bind_owner(cluster.elect()):
        with pytest.raises(RemoteUncertain, match="not visible"):
            release.dispatch(request, standby, config, lambda: provider, http)
        assert http.writes == 1
        http.hidden = False
        http.trusted_sha = "d" * 40  # Main advanced after the send and local snapshot.
        recovered = release.dispatch(request, standby, config, lambda: provider, http)
        assert recovered["state"] == "observed" and recovered["run_id"] == "712"
        assert release.dispatch(request, standby, config, lambda: provider, http)["run_id"] == "712"
    assert http.writes == 1


def test_quorum_owner_expiry_after_post_never_reports_acceptance(release_candidate):
    provider, config, _, store, report = release_candidate
    request = release.plan(report, 12, "main")
    http, cluster = WorkflowHTTP(provider), Cluster()
    owner = cluster.elect()
    http.after_post = lambda: setattr(owner, "deadline", 0)
    with bind_owner(owner), pytest.raises(OwnershipLost):
        release.dispatch(request, store, config, lambda: provider, http)
    assert http.writes == 1
    local = store.path.parent / "releases" / f"{request['request_id']}.json"
    assert json.loads(local.read_text())["state"] == "uncertain"
    with bind_owner(cluster.elect()):
        assert (
            release.dispatch(request, store, config, lambda: provider, http)["state"] == "observed"
        )
    assert http.writes == 1


def test_lost_reservation_reply_cannot_dispatch_or_reopen_slot(release_candidate, monkeypatch):
    provider, config, _, store, report = release_candidate
    request = release.plan(report, 12, "main")
    http, cluster = WorkflowHTTP(provider), Cluster()
    original = cluster.compare
    dropped = []

    def compare(checks, puts, **kwargs):
        result = original(checks, puts, **kwargs)
        if result and any(key.startswith("effects/") for key in puts) and not dropped:
            dropped.append(copy.deepcopy(puts))
            raise RemoteUncertain("lost reservation reply")
        return result

    monkeypatch.setattr(cluster, "compare", compare)
    with bind_owner(cluster.elect()):
        assert (
            release.dispatch(request, store, config, lambda: provider, http)["state"] == "uncertain"
        )
    assert http.writes == 0 and dropped
    with bind_owner(cluster.elect()):
        assert (
            release.dispatch(request, store, config, lambda: provider, http)["state"] == "uncertain"
        )
    assert http.writes == 0


def test_coordinated_retry_uncertain_cannot_override_quorum_reservation(release_candidate):
    provider, config, _, store, report = release_candidate
    request = release.plan(report, 12, "main")
    http, cluster = WorkflowHTTP(provider), Cluster()
    http.lose_reply = http.hidden = True
    with bind_owner(cluster.elect()):
        release.dispatch(request, store, config, lambda: provider, http)
    with bind_owner(cluster.elect()), pytest.raises(RemoteUncertain, match="not visible"):
        release.dispatch(request, store, config, lambda: provider, http, retry_uncertain=True)
    assert http.writes == 1
