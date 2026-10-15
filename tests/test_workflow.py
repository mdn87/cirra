import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def invoke(state, *extra):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cirra",
            "review",
            "--config",
            str(ROOT / "examples/policy.yaml"),
            "--fixture",
            str(ROOT / "examples/change.json"),
            "--state-dir",
            str(state),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_offline_review_exports_report_and_reuses_analysis(tmp_path):
    first = invoke(tmp_path)
    assert first.returncode == 0, first.stderr
    receipt = json.loads(first.stdout)
    report = json.loads(Path(receipt["json_path"]).read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["advisory"] is True
    assert report["ci"]["outcome"] == "verified"
    assert report["review"]["outcome"] == "succeeded"
    assert len(report["review"]["findings"]) == 1
    assert "Advisory" in Path(receipt["markdown_path"]).read_text(encoding="utf-8")

    for _ in range(10):
        repeated = invoke(tmp_path)
        assert repeated.returncode == 0, repeated.stderr
        reused = json.loads(repeated.stdout)
        assert reused["run_id"] == receipt["run_id"]
        assert reused["reused"] is True

    fresh = invoke(tmp_path, "--rereview")
    assert fresh.returncode == 0, fresh.stderr
    fresh_receipt = json.loads(fresh.stdout)
    assert fresh_receipt["run_id"] != receipt["run_id"]
    fresh_report = json.loads(Path(fresh_receipt["json_path"]).read_text(encoding="utf-8"))
    assert fresh_report["review"]["attempt"] == 2

    shown = subprocess.run(
        [
            sys.executable,
            "-m",
            "cirra",
            "show",
            "--state-dir",
            str(tmp_path),
            "--run",
            receipt["run_id"],
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["run_id"] == receipt["run_id"]
