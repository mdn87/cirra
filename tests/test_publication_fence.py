import copy
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_coordination_extensions import captured

from cirra.audit_worker import AuditCheckPublisher
from cirra.coordinator import review
from cirra.domain import CirraError, ProviderAccessDenied, digest
from cirra.observer import ObserverCheck, publish_observation
from cirra.publication_fence import PublicationFence, require_send
from cirra.publish import GitHubPublisher, GitLabPublisher, publish
from cirra.quorum import OWNER_KEY, Owner, bind_owner
from cirra.review_gate import RequiredReviewCheck
from cirra.store import Store
from cirra.transport import RemoteUncertain


class Cluster:
    def __init__(self):
        self.values = {}

    def elect(self):
        owner = Owner(self)
        self.values[OWNER_KEY] = owner.token
        return owner

    def get(self, key):
        return self.values.get(key)

    def compare(self, checks, puts, *, deletes=()):
        if not all(self.values.get(key) == expected for key, expected in checks.items()):
            return False
        self.values.update(puts)
        for key in deletes:
            self.values.pop(key, None)
        return True


class Provider:
    def __init__(self):
        self.item = None
        self.hidden = self.lose_reply = False
        self.writes = []

    def response(self):
        if self.lose_reply:
            raise RemoteUncertain("lost reply")
        return copy.deepcopy(self.item)

    def visible(self):
        return [] if self.hidden or self.item is None else [copy.deepcopy(self.item)]

    def request(self, method, path, params=None, *, body=None):
        if method == "GET":
            return {"check_runs": self.visible()}, False
        self.writes.append(method)
        if method == "POST":
            self.item = {
                "id": 11,
                "app": {"id": 42},
                "html_url": "https://github.com/example/repo/runs/11",
                "check_suite": {"id": 9},
                "conclusion": None,
            }
        self.item.update(copy.deepcopy(body))
        return self.response(), False

    def get(self, path, params=None):
        return self.visible(), False

    def note(self, change, body, identifier=None):
        self.writes.append("PUT" if identifier else "POST")
        self.item = {"id": 11, "author": {"id": 42}, "system": False, "body": body}
        return self.response()


def test_delayed_ack_cannot_reopen_a_new_generation_or_issue_a_second_send():
    cluster = Cluster()
    first = cluster.elect()
    with bind_owner(first):
        initial = PublicationFence("advisory", "private-slot")
        initial.observe(None, None)
        with initial.writing("original", None):
            require_send("advisory", "private-slot", "original", None)
            with pytest.raises(RemoteUncertain, match="reservation"):
                require_send("advisory", "private-slot", "original", None)
            with bind_owner(cluster.elect()):
                active = PublicationFence("advisory", "private-slot")
                with pytest.raises(RemoteUncertain, match="not visible"):
                    active.observe(None, None)
                with pytest.raises(RemoteUncertain, match="differs"):
                    active.observe("11", "stale")
                assert active.observe("11", "original")
                with active.writing("replacement", "11"):
                    with pytest.raises(RemoteUncertain, match="different native"):
                        active.acknowledged("replacement", "12", target="11")
                    active.acknowledged("replacement", "11", target="11")
            with pytest.raises(RemoteUncertain):
                initial.acknowledged("original", "11", target=None)
    for key, value in cluster.values.items():
        if key != OWNER_KEY:
            assert re.fullmatch("effects/[0-9a-f]{64}", key)
            assert re.fullmatch("[0-9a-f]{64}", value)


def test_confirmed_refusal_restores_prior_slot_and_legacy_objects_cannot_disappear():
    cluster = Cluster()
    with bind_owner(cluster.elect()):
        fence = PublicationFence("review", "slot")
        assert not fence.observe("11", "legacy")
        with pytest.raises(ProviderAccessDenied), fence.writing("candidate", "11"):
            raise ProviderAccessDenied("provider refused before applying")
        assert not fence.observe("11", "legacy")
        with pytest.raises(RemoteUncertain, match="not visible"):
            fence.observe(None, None)
        with pytest.raises(RemoteUncertain, match="differs"):
            fence.observe("12", "legacy")


def test_a_fence_created_without_ownership_cannot_send_inside_a_managed_worker():
    fence = PublicationFence("review", "slot")
    with bind_owner(Cluster().elect()):
        with pytest.raises(RemoteUncertain, match="ownership"), fence.writing("body", None):
            pytest.fail("attempted to send")


@pytest.mark.parametrize(
    ("marker", "value"),
    [("worker-queue-format.json", '{"format": 2}'), ("coordination-state.json", "{}")],
)
def test_enrolled_state_rejects_direct_advisory_publication_without_an_owner(
    tmp_path, marker, value
):
    (tmp_path / marker).write_text(value)
    with pytest.raises(CirraError, match="active quorum worker"):
        publish(Store(tmp_path), None, "run", None, None)


@pytest.mark.parametrize("provider_name", ["github", "gitlab"])
def test_advisory_recovery_uses_exact_native_body_across_private_state_roots(
    inputs, fixture_data, tmp_path, provider_name
):
    provider, config = captured(inputs, fixture_data)
    provider.snapshot = replace(
        provider.snapshot, provider=provider_name, instance=f"https://{provider_name}.com"
    )
    primary, standby = Store(tmp_path / "primary"), Store(tmp_path / "standby")
    report, _ = review(provider, config, primary)
    with primary.connection() as source, standby.connection() as destination:
        source.backup(destination)
    api = Provider()
    writer = (
        GitHubPublisher("example/repo", 42, "fixture", http=api)
        if provider_name == "github"
        else GitLabPublisher(api, 42)
    )
    cluster = Cluster()
    with bind_owner(cluster.elect()):
        with pytest.raises(CirraError, match="supported native"):
            publish(
                primary,
                config,
                report["run_id"],
                lambda: provider,
                SimpleNamespace(principal_id="42"),
            )
        with pytest.raises(RemoteUncertain, match="reservation"):
            writer.write(report, {"slot": "example"})
        api.lose_reply = True
        receipt = publish(primary, config, report["run_id"], lambda: provider, writer)
        assert receipt["state"] == "uncertain"
    with bind_owner(cluster.elect()):
        api.lose_reply, api.hidden = False, True
        with pytest.raises(RemoteUncertain, match="not visible"):
            publish(standby, config, report["run_id"], lambda: provider, writer)
        api.hidden = False
        exact = copy.deepcopy(api.item)
        if provider_name == "github":
            api.item["output"]["summary"] += " altered"
        else:
            api.item["body"] += " altered"
        with pytest.raises(RemoteUncertain, match="differs"):
            publish(standby, config, report["run_id"], lambda: provider, writer)
        api.item = exact
        result = publish(
            standby, config, report["run_id"], lambda: provider, writer, reconcile=True
        )
        assert result["state"] == "published" and result["native_id"] == "11"
        assert (
            publish(standby, config, report["run_id"], lambda: provider, writer)["state"]
            == "published"
        )
        divergent = Store(tmp_path / "divergent")
        other, _ = review(provider, config, divergent)
        with pytest.raises(CirraError, match="cannot order"):
            publish(divergent, config, other["run_id"], lambda: provider, writer)
        assert api.writes == ["POST"]


def test_required_gate_cannot_send_over_an_unconfirmed_completion(tmp_path):
    api, cluster = Provider(), Cluster()
    route = SimpleNamespace(
        repository="example/repo",
        repository_id="42",
        publisher={"app_id": 42},
        policy=SimpleNamespace(digest="policy"),
    )
    head = "a" * 40

    def gate(directory):
        return RequiredReviewCheck(
            directory, route, {"head_sha": head}, lambda: api, lambda: "b" * 40
        )

    with bind_owner(cluster.elect()):
        primary = gate(tmp_path / "primary")
        primary.start()
        old = copy.deepcopy(api.item)
        api.lose_reply = True
        with pytest.raises(RemoteUncertain):
            primary.send(
                api,
                "PATCH",
                "/repos/example/repo/check-runs/11",
                {
                    "status": "completed",
                    "conclusion": "success",
                    "output": {"title": "Review", "summary": "ok"},
                },
                {"state": "published", "conclusion": "success", "native_id": "11"},
            )
    with bind_owner(cluster.elect()):
        survivor = gate(tmp_path / "standby")
        exact, api.item, api.lose_reply = api.item, old, False
        with pytest.raises(RemoteUncertain, match="differs"):
            survivor.start()
        api.item = exact
        survivor.start()
        assert survivor.identifier == "11" and api.writes == ["POST", "PATCH"]


def test_audit_uses_its_original_creation_marker_after_host_loss():
    api, cluster = Provider(), Cluster()
    publisher = AuditCheckPublisher("example/repo", 42, "fixture", http=api)
    args = ("a" * 40, "audit-id", "success", "synthetic audit")
    with bind_owner(cluster.elect()), pytest.raises(RemoteUncertain):
        api.lose_reply = True
        publisher.publish(*args)
    with bind_owner(cluster.elect()):
        api.lose_reply, api.hidden = False, True
        with pytest.raises(RemoteUncertain, match="not visible"):
            publisher.publish(*args)
        api.hidden = False
        marker = api.item["output"].pop("text")
        with pytest.raises(RemoteUncertain, match="differs"):
            publisher.publish(*args)
        api.item["output"]["text"] = marker
        assert publisher.publish(*args) == {"native_id": "11", "existing": True}
        assert api.writes == ["POST"]


def test_observer_requires_the_original_execution_and_never_creates_from_an_empty_read(tmp_path):
    api, cluster = Provider(), Cluster()
    publisher = ObserverCheck("example/repo", 42, "fixture", http=api)
    head, definition = "a" * 40, "b" * 64
    saved = {"state": "executed", "publication_id": "c" * 32, "started_at": "2026-11-01T00:00:00Z"}
    slot = digest(["execution"])

    def send(local_state):
        return publish_observation(
            publisher,
            {"head_sha": head},
            slot,
            "success",
            local_state,
            tmp_path / "record.json",
            {"definition_digest": definition},
        )

    with bind_owner(cluster.elect()), pytest.raises(RemoteUncertain):
        api.lose_reply = True
        send(saved.copy())
    with bind_owner(cluster.elect()):
        api.hidden, api.lose_reply = True, False
        with pytest.raises(RemoteUncertain, match="not visible"):
            send(saved.copy())
        api.hidden = False
        with pytest.raises(RemoteUncertain, match="execution"):
            send({**saved, "publication_id": "d" * 32})
        assert send(saved.copy())["native_id"] == "11" and api.writes == ["POST"]
