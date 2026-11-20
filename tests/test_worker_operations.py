"""Durability, admission, recovery and retention at the service entry points."""

import io
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cirra.domain import CirraError, ProviderAccessDenied, ProviderRateLimited
from cirra.health import check, queue_metrics
from cirra.job_queue import Queue
from cirra.publish import write_receipt
from cirra.reconcile import sync
from cirra.retention import cold_evidence, manifest
from cirra.worker import heartbeat, work_once


def route(name="first", credential="READER"):
    return SimpleNamespace(
        name=name,
        digest=name + "-v1",
        provider="github",
        instance="https://github.com",
        reader_env=credential,
    )


def hint(value, *, change=1):
    return {"change": change, "delivery": value, "payload_digest": value}


def test_receipt_syncs_new_directory_entries_after_file_replacement(tmp_path, monkeypatch):
    from cirra import publish

    path = tmp_path / "queue" / "job.json"
    seen = []
    monkeypatch.setattr(
        publish, "sync_directory", lambda parent: seen.append((parent, path.exists()))
    )
    write_receipt(path, {"state": "done"})
    assert (tmp_path, False) in seen
    assert (path.parent / "history" / "job", False) in seen
    assert seen[-1] == (path.parent, True)


def test_recover_interrupted_accept_and_replay_after_off_host_archive(tmp_path, monkeypatch):
    queue, target = Queue(tmp_path, 1), route()
    save = queue.save

    def interrupt(job):
        raise OSError("disk unavailable")

    monkeypatch.setattr(queue, "save", interrupt)
    with pytest.raises(OSError):
        queue.enqueue(target, hint("first"))
    assert len(list((queue.directory / "deliveries").glob("*.json"))) == 1
    assert queue.pending() == []
    monkeypatch.setattr(queue, "save", save)
    assert not queue.enqueue(target, hint("first"))["duplicate"]
    (job,) = queue.pending()
    job["state"] = "done"
    queue.save(job)
    assert queue.enqueue(target, hint("next"))["duplicate"] is False
    archived = queue.directory / "archive" / (job["id"] + ".json")
    assert archived.is_file()
    archived.replace(tmp_path / "cold.json")
    assert queue.enqueue(target, hint("first"))["duplicate"]
    assert queue.enqueue(target, {**hint("first"), "delivery": "another"})["duplicate"]
    with pytest.raises(CirraError, match="reused"):
        queue.enqueue(target, {**hint("new"), "delivery": "another"})
    assert len(queue.pending()) == 1


def test_legacy_version_moves_forward_and_malformed_job_does_not_block(tmp_path):
    queue, target = Queue(tmp_path), route()
    queue.enqueue(target, hint("good"))
    queue.enqueue(target, hint("bad"))
    jobs, _ = queue.scan()
    good, bad = sorted(jobs, key=lambda j: j["hint"]["delivery"], reverse=True)
    good.pop("schema_version")
    write_receipt(queue.directory / (good["id"] + ".json"), good)
    bad["hint"]["change"] = []
    write_receipt(queue.directory / (bad["id"] + ".json"), bad)
    assert [j["id"] for j in queue.pending()] == [good["id"]]
    assert queue.scan()[1] == [bad["id"] + ".json"]
    queue.save(good)
    assert queue.scan()[0][0]["schema_version"] == 1
    bad["hint"]["change"], bad["schema_version"] = 1, 2
    write_receipt(queue.directory / (bad["id"] + ".json"), bad)
    assert queue.scan()[1] == [bad["id"] + ".json"]


@pytest.mark.parametrize("limited", [False, True])
def test_preflight_refusal_defers_shared_reader_but_keeps_healthy_route_moving(tmp_path, limited):
    queue = Queue(tmp_path)
    routes = [route(), route("second"), route("healthy", "OTHER_READER")]
    config = SimpleNamespace(
        routes={r.name: r for r in routes}, max_attempts=2, retry_seconds=1, max_jobs_per_day=1
    )
    for r in routes:
        queue.enqueue(r, hint(r.name))

    def process(r, event):
        if r.name == "first":
            err = (
                ProviderRateLimited("provider quota", time.time() + 3600)
                if limited
                else ProviderAccessDenied("provider denied")
            )
            err.before_execution = True
            raise err
        assert r.name == "healthy"
        return {"state": "done"}

    first = work_once(config, queue, process)
    assert first["state"] == ("retry" if limited else "attention")
    assert queue.usage()["attempts"] == 0
    assert work_once(config, queue, process)["route"] == "healthy"
    assert queue.usage()["attempts"] == 1
    assert work_once(config, queue, process) is None
    seen = []
    assert (
        sum(
            "deferred_until" in result
            for result in sync(
                config,
                queue,
                lambda _: "unused",
                discover_changes=lambda r, _: seen.append(r.name) or [],
            )
        )
        == 2
    )
    assert seen == ["healthy"]


def test_capacity_refusal_oldest_age_and_delivery_objectives(tmp_path):
    queue, target = Queue(tmp_path, 2), route()
    heartbeat(queue)
    queue.enqueue(target, hint("finished"))
    queue.enqueue(target, hint("waiting"))
    now = datetime.now(UTC)
    for job in queue.pending():
        job["accepted_at"] = (now - timedelta(minutes=50)).isoformat()
        if job["hint"]["delivery"] == "finished":
            job.update(state="done", updated_at=(now - timedelta(minutes=35)).isoformat())
        queue.save(job)
    assert queue.enqueue(target, hint("fresh"))["duplicate"] is False
    assert queue.enqueue(target, hint("finished"))["duplicate"]
    with pytest.raises(CirraError, match="capacity"):
        queue.enqueue(target, hint("refused"))
    metrics = queue_metrics(queue)
    assert metrics["remaining"] == 0
    assert metrics["admissions_today"] == {"accepted": 3, "duplicate": 1, "refused": 1}
    assert metrics["acceptance_percent_today"] == 80
    assert 3000 <= metrics["oldest_eligible_seconds"] < 3010
    assert metrics["webhook_arrivals_24h"] == 3 and metrics["webhook_completions_24h"] == 1
    assert metrics["eligible_webhooks_24h"] == 2 and metrics["completed_within_30m"] == 1
    assert metrics["on_time_percent"] == 50
    assert {"admission", "backlog", "capacity"} <= {p["kind"] for p in check(tmp_path)["problems"]}
    queue.defer_reader(target, ProviderRateLimited("quota", time.time() + 1800))
    assert queue_metrics(queue)["oldest_eligible_seconds"] == 0


def test_cold_evidence_lists_only_completed_old_source_and_immutable_records(tmp_path):
    queue, target = Queue(tmp_path), route()
    queue.enqueue(target, hint("one"))
    (job,) = queue.pending()
    job["state"] = "done"
    queue.save(job)
    queue.archive_finished([job], pressure=True)
    stamp = time.time() - 31 * 86400
    observer = tmp_path / "observer"
    observer.mkdir()
    old, active = "a" * 64, "b" * 64
    for alias, status in ((old, "observed"), (active, "running")):
        write_receipt(observer / f"{alias}.json", {"state": status}, history=False)
        directory = observer / (alias + "-" + "f" * 32)
        directory.mkdir()
        (directory / "source.tar").write_bytes(b"fixture")
        (directory / "container.log").write_bytes(b"retained evidence")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))
    keep = list(cold_evidence(tmp_path))
    names = {p.as_posix() for p in keep}
    assert f"queue/archive/{job['id']}.json" in names
    assert any(n.startswith("queue/history/") for n in names)
    assert any(n.startswith("observer/" + old) and n.endswith("/source.tar") for n in names)
    assert all(
        not n.startswith("observer/" + active)
        and not n.endswith("container.log")
        and "/completed/" not in n
        and "/deliveries/" not in n
        for n in names
    )
    out = io.BytesIO()
    manifest(tmp_path, 30, out)
    assert set(out.getvalue().decode().split("\0")) == names | {""}
