import subprocess

from cirra.coordinator import review
from cirra.store import Store


def test_actual_git_patch_is_reviewed_as_data(inputs, fixture_data, tmp_path):
    repository = tmp_path / "fixture-repository"
    repository.mkdir()

    def git(*args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Cirra fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.autocrlf=false",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()

    git("init", "--initial-branch=main")
    source = repository / "arithmetic.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git("add", "arithmetic.py")
    git("commit", "-m", "fixture: correct arithmetic")
    base = git("rev-parse", "HEAD")
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    git("add", "arithmetic.py")
    git("commit", "-m", "fixture: introduce regression")
    head = git("rev-parse", "HEAD")
    patch = git("diff", "--no-ext-diff", "--no-textconv", base, head, "--", "arithmetic.py")
    fixture_data["change"].update(head_sha=head, diff_base_sha=base, target_sha=base)
    fixture_data["files"][0].update(path="arithmetic.py", patch=patch[patch.index("@@") :])
    fixture_data["reviewer_response"]["findings"][0]["path"] = "arithmetic.py"
    check = fixture_data["checks"][0]
    check["associated_sha"] = head
    check["provenance"]["tested_sha"] = head
    provider, config = inputs(fixture_data)
    report, _ = review(provider, config, Store(tmp_path / "state"))
    assert report["snapshot"]["head_sha"] == head
    assert report["status"] == "complete"
    assert report["review"]["findings"][0]["start_line"] == 2
