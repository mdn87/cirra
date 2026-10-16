import copy
import json
import subprocess
import tarfile

import pytest
import yaml

from cirra import provenance
from cirra.domain import CirraError
from cirra.github import check_from
from cirra.observer import NAME, DockerExecutor, load_observer, observe
from cirra.observer_process import run_control
from cirra.observer_source import export_source
from cirra.transport import RemoteUncertain

KEY = "independent-observer-signing-key-at-least-32-bytes"
IMAGE = "sha256:" + "1" * 64


@pytest.fixture
def observer_policy(tmp_path):
    path = tmp_path / "observer.yaml"
    path.write_text(
        yaml.safe_dump(
            {"version": 1, "image": IMAGE, "commands": [["python", "-m", "pytest", "-q"]]}
        )
    )
    return load_observer(path)


class CheckPublisher:
    app_id = "42"

    def __init__(self):
        self.items, self.calls, self.lose_ack, self.hide = [], 0, False, False

    def find(self, head, identifier, result):
        return [] if self.hide else copy.deepcopy(self.items)

    def create(self, head, identifier, result, started_at, definition):
        self.calls += 1
        item = {
            "id": 100,
            "name": NAME,
            "app": {"id": 42},
            "head_sha": head,
            "external_id": identifier,
            "status": "completed",
            "conclusion": result,
            "started_at": started_at,
            "html_url": "https://github.com/example/repo/runs/100",
            "check_suite": {"id": 200},
        }
        self.items = [item]
        if self.lose_ack:
            raise RemoteUncertain("lost acknowledgement")
        return item


class Executor:
    def __init__(self, exit_code=0):
        self.calls, self.exit_code = 0, exit_code

    def run(self, policy, archive, directory, name):
        self.calls += 1
        return {"container_id": "a" * 64, "image_id": IMAGE, "exit_code": self.exit_code}

    def recover(self, policy, saved, directory):
        self.recovered = saved["container_name"]


def source(repository, change, expected, directory, token, *, provider="github"):
    directory.mkdir()
    archive = directory / "source.tar"
    archive.write_bytes(b"verified archive")
    return archive, ["b" * 40]


def data_for_observer(fixture_data):
    data = copy.deepcopy(fixture_data)
    data["change"].update(provider="github", instance="https://github.com", repository_id="42")
    data["collection"] = {"repository": "example/repo"}
    return data


@pytest.mark.parametrize("exit_code,conclusion", [(0, "success"), (1, "failure")])
def test_independent_execution_produces_bound_witness_and_reuses_result(
    fixture_data, observer_policy, tmp_path, exit_code, conclusion
):
    data, executor, publisher = (
        data_for_observer(fixture_data),
        Executor(exit_code),
        CheckPublisher(),
    )
    receipt = observe(
        data, observer_policy, tmp_path / "state", KEY, publisher, executor=executor, source=source
    )
    check = check_from(
        publisher.items[0], "check_run", data["change"]["head_sha"], "2026-10-15T00:00:00Z"
    )
    assert check["raw_state"] == conclusion
    data["checks"] = [check]
    verified = provenance.apply(data, receipt, KEY)["checks"][0]
    assert verified["provenance"]["trusted"]
    assert verified["provenance"]["tested_sha"] == data["change"]["head_sha"]
    assert verified["provenance"]["definition_digest"] == observer_policy["definition_digest"]
    observe(
        data, observer_policy, tmp_path / "state", KEY, publisher, executor=executor, source=source
    )
    assert executor.calls == publisher.calls == 1
    check["native_id"] = "201"
    assert not provenance.apply(data, receipt, KEY)["checks"][0]["provenance"]["trusted"]


def test_lost_native_ack_reconciles_without_reexecuting_or_creating_duplicate(
    fixture_data, observer_policy, tmp_path
):
    data, executor, publisher = data_for_observer(fixture_data), Executor(), CheckPublisher()
    publisher.lose_ack = True
    with pytest.raises(RemoteUncertain):
        observe(data, observer_policy, tmp_path, KEY, publisher, executor=executor, source=source)
    publisher.hide = True
    with pytest.raises(CirraError, match="uncertain"):
        observe(data, observer_policy, tmp_path, KEY, publisher, executor=executor, source=source)
    publisher.hide = False
    assert observe(
        data, observer_policy, tmp_path, KEY, publisher, executor=executor, source=source
    ).exists()
    assert executor.calls == publisher.calls == 1


def test_interrupted_execution_does_not_fabricate_attestation(
    fixture_data, observer_policy, tmp_path
):
    class Interrupted:
        def run(self, *args):
            raise SystemExit("process died")

    data, publisher = data_for_observer(fixture_data), CheckPublisher()
    with pytest.raises(SystemExit):
        observe(
            data, observer_policy, tmp_path, KEY, publisher, executor=Interrupted(), source=source
        )
    assert publisher.calls == 0
    assert not list((tmp_path / "observer").glob("*-witness.json"))
    executor = Executor(exit_code=1)
    receipt = observe(
        data, observer_policy, tmp_path, KEY, publisher, executor=executor, source=source
    )
    assert executor.calls == 1 and executor.recovered.startswith("cirra-observer-")
    assert receipt.exists() and publisher.items[0]["conclusion"] == "failure"


def test_docker_launch_isolated_and_pinned(observer_policy, tmp_path, monkeypatch):
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"archive")
    calls = []

    def run(argv, directory, **kwargs):
        assert argv[1:3] == ["--context", observer_policy["docker_context"]]
        argv = [argv[0], *argv[3:]]
        calls.append(argv)
        if argv[1] == "context":
            return b'"unix:///var/run/docker.sock"'
        if argv[1] == "info":
            return b'{"OSType":"linux"}'
        if argv[1:3] == ["image", "inspect"]:
            return json.dumps([{"Id": IMAGE, "Os": "linux"}]).encode()
        if argv[1] == "create":
            return b"a" * 64
        if argv[1] == "wait":
            return b"0"
        if argv[1] == "inspect":
            return json.dumps(
                [{"Id": "a" * 64, "State": {"Running": False, "ExitCode": 0}}]
            ).encode()
        return b""

    monkeypatch.setattr("cirra.observer.run_control", run)
    monkeypatch.setenv("CIRRA_WITNESS_KEY", KEY)
    result = DockerExecutor().run(observer_policy, archive, tmp_path, "cirra-observer-test")
    create = next(argv for argv in calls if argv[1] == "create")
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--user=65534:65534",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--pull=never",
    ):
        assert flag in create
    assert IMAGE in create and KEY not in str(create)
    assert len([arg for arg in create if arg == "--mount"]) == 1
    assert not any("docker.sock" in arg or ".env" in arg for arg in create)
    assert result["exit_code"] == 0 and calls[-1][1:3] == ["rm", "--force"]


@pytest.mark.parametrize("provider,ref", [("github", "pull"), ("gitlab", "merge-requests")])
def test_real_git_export_ignores_attributes_and_never_checks_out_code(
    tmp_path, monkeypatch, provider, ref
):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                *args,
            ],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout

    git("init")
    (repository / ".gitattributes").write_text("test.py export-ignore\n*.py filter=host-command\n")
    (repository / "test.py").write_text("raise RuntimeError('must only run in the sandbox')\n")
    git("add", ".")
    git("commit", "-m", "fixture")
    head = git("rev-parse", "HEAD").decode().strip()
    real_run = run_control

    def redirected(argv, directory, **kwargs):
        if "fetch" in argv:
            assert argv[-2:] == [f"https://{provider}.com/example/repo.git", f"refs/{ref}/1/head"]
            assert "reader-canary" not in str(argv)
            env = kwargs["env"]
            headers = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
            assert f"http.https://{provider}.com/example/repo.git.extraHeader" in headers
            # Keep the real git export; only replace the transport for this local fixture.
            argv = [
                "git",
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--depth=1",
                str(repository),
                "HEAD",
            ]
        return real_run(argv, directory, **kwargs)

    monkeypatch.setattr("cirra.observer_source.run_control", redirected)
    archive, parents = export_source(
        "example/repo", 1, head, tmp_path / "export", "reader-canary", provider=provider
    )
    assert parents == []
    with tarfile.open(archive) as tar:
        assert "test.py" in tar.getnames()
        assert tar.extractfile("test.py").read().startswith(b"raise RuntimeError")
    assert not (archive.parent / "git" / "test.py").exists()
    with pytest.raises(CirraError, match="head moved"):
        export_source(
            "example/repo", 1, "f" * 40, tmp_path / "wrong-head", "reader-canary", provider=provider
        )


def test_source_symlinks_rejected_before_container_execution(tmp_path, monkeypatch):
    def fake_git(argv, directory, **kwargs):
        if "rev-parse" in argv:
            return b"a" * 40
        if "cat-file" in argv:
            return b"tree " + b"b" * 40 + b"\n\nmessage"
        if "ls-tree" in argv:
            return b"120000 blob " + b"c" * 40 + b" 10\tlink\0"
        return b""

    monkeypatch.setattr("cirra.observer_source.run_control", fake_git)
    with pytest.raises(CirraError, match="symlinks"):
        export_source("example/repo", 1, "a" * 40, tmp_path / "export")


def test_policy_definition_changes_with_image_or_commands(observer_policy, tmp_path):
    original = observer_policy["definition_digest"]
    path = tmp_path / "observer.yaml"
    value = yaml.safe_load(path.read_text())
    value["commands"] = [["python", "-m", "pytest", "tests/test_workflow.py"]]
    path.write_text(yaml.safe_dump(value))
    assert load_observer(path)["definition_digest"] != original
    value["image"] = "python:latest"
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(CirraError, match="immutable"):
        load_observer(path)


@pytest.mark.parametrize("exit_code,outcome", [(0, "verified"), (1, "failed")])
def test_gitlab_observer_verifies_own_result_without_attesting_native_pipeline(
    fixture_data, observer_policy, tmp_path, inputs, policy_data, exit_code, outcome
):
    from cirra.policy import assess_checks

    data = data_for_observer(fixture_data)
    data["change"].update(provider="gitlab", instance="https://gitlab.com")
    data["collection"].update(
        source="gitlab_rest", observed_at="2026-10-16T00:00:00Z", check_errors=[]
    )
    data["checks"][0].update(
        kind="pipeline_job", provenance={"trusted": False, "tested_sha": None, "parents": []}
    )
    executor = Executor(exit_code)
    receipt = observe(data, observer_policy, tmp_path, KEY, executor=executor, source=source)
    verified = provenance.apply(data, receipt, KEY)
    assert verified["checks"][0] == data["checks"][0]
    assert verified["checks"][1]["kind"] == "observer_run"
    assert verified["checks"][1]["provenance"]["trusted"]
    assert provenance.apply(verified, receipt, KEY) == verified
    policy_data["repositories"] = [
        {key: data["change"][key] for key in ("provider", "instance", "repository_id")}
    ]
    policy_data["ci"]["expected"] = [
        {
            "kind": "observer_run",
            "source_id": "cirra/docker",
            "name": NAME,
            "definition_digest": observer_policy["definition_digest"],
        }
    ]
    provider, config = inputs(data=verified, policy=policy_data)
    assert assess_checks(provider.checks, provider.snapshot, config)["outcome"] == outcome
    observe(data, observer_policy, tmp_path, KEY, executor=executor, source=source)
    assert executor.calls == 1
    for field, value in (
        ("head_sha", "f" * 40),
        ("target_sha", "e" * 40),
        ("diff_base_sha", "d" * 40),
        ("repository_id", "99"),
        ("change_id", "2"),
        ("instance", "https://other.invalid"),
        ("provider", "github"),
    ):
        moved = copy.deepcopy(data)
        moved["change"][field] = value
        with pytest.raises(CirraError, match="snapshot"):
            provenance.apply(moved, receipt, KEY)
    with pytest.raises(CirraError, match="signature"):
        provenance.apply(data, receipt, KEY + "wrong")
    raw = json.loads(receipt.read_text())
    raw["statement"]["checks"][0]["evidence"] = "forged execution"
    receipt.write_text(json.dumps(raw))
    with pytest.raises(CirraError, match="signature"):
        provenance.apply(data, receipt, KEY)


def test_standalone_witness_cannot_invent_a_native_pipeline_or_omit_execution_definition(
    fixture_data, observer_policy, tmp_path
):
    data = data_for_observer(fixture_data)
    data["change"].update(provider="gitlab", instance="https://gitlab.com")
    receipt = observe(data, observer_policy, tmp_path, KEY, executor=Executor(), source=source)
    original = json.loads(receipt.read_text())["statement"]
    for kind in ("pipeline_job", "commit_status", "check_run"):
        value = copy.deepcopy(original)
        item = value["checks"][0]
        item["check"]["kind"] = kind
        item["binding"] = provenance.binding(value["change"], item["check"])
        with pytest.raises(CirraError, match="own exact observer"):
            provenance.sign(value, KEY)
    del original["checks"][0]["definition_digest"]
    with pytest.raises(CirraError, match="own exact observer"):
        provenance.sign(original, KEY)


def test_gitlab_target_refresh_resigns_receipt_without_rerunning_head_tests(
    fixture_data, observer_policy, tmp_path
):
    data = data_for_observer(fixture_data)
    data["change"].update(provider="gitlab", instance="https://gitlab.com")
    executor = Executor()
    path = observe(data, observer_policy, tmp_path, KEY, executor=executor, source=source)
    original = path.read_bytes()
    moved = copy.deepcopy(data)
    moved["change"].update(target_sha="d" * 40, diff_base_sha="e" * 40)
    with pytest.raises(CirraError, match="snapshot"):
        provenance.apply(moved, path, KEY)
    refreshed = observe(moved, observer_policy, tmp_path, KEY, executor=executor, source=source)
    assert executor.calls == 1 and path == refreshed and path.read_bytes() != original
    assert provenance.apply(moved, refreshed, KEY)["checks"][-1]["provenance"]["trusted"]
    with pytest.raises(CirraError, match="snapshot"):
        provenance.apply(data, refreshed, KEY)
