"""Frozen labelled review cases; labels are never part of the reviewer packet."""

import json
from pathlib import Path

from cirra.coordinator import review
from cirra.dispatch import INSTRUCTIONS, PROTOCOL
from cirra.domain import CirraError, canonical, digest, number, utc_now
from cirra.fixture import FixtureProvider
from cirra.report import export_report
from cirra.store import Store


def matches(finding, label):
    text = (finding["title"] + " " + finding["evidence"]).lower()
    return (
        finding["path"] == label["path"]
        and finding["side"] == label["side"]
        and finding["start_line"] <= label["end_line"]
        and finding["end_line"] >= label["start_line"]
        and all(any(term.lower() in text for term in group) for group in label["concepts"])
    )


def score(case, report):
    findings, labels = report["review"]["findings"], case["labels"]
    matched, used = [], set()
    duplicates = 0
    for label in labels:
        candidates = [i for i, finding in enumerate(findings) if matches(finding, label)]
        matched.append(bool(candidates))
        if candidates:
            used.add(candidates[0])
            duplicates += max(0, len(candidates) - 1)
    spurious = [finding for i, finding in enumerate(findings) if i not in used]
    return {
        "case_id": case["id"],
        "split": case["split"],
        "defective": bool(labels),
        "injection": case.get("injection", False),
        "detected": bool(labels) and all(matched),
        "misses": [label["id"] for label, found in zip(labels, matched, strict=True) if not found],
        "spurious_findings": spurious,
        "duplicates": duplicates,
        "usefulness": "requires human adjudication",
        "run_id": report["run_id"],
        "outcome": report["review"]["outcome"],
        "duration_seconds": report["review"]["duration_seconds"],
        "usage": report["review"].get("usage"),
        "cost": report["review"].get("cost"),
        "api_equivalent_cost_usd": report["review"].get("api_equivalent_cost_usd"),
        "resolved_models": report["review"].get("resolved_models", []),
        "review": report["review"],
    }


def evaluate(corpus_path, config, state_dir, *, rounds=2):
    if config.reviewer_kind != "claude":
        raise CirraError("quality evaluation requires an automatic real reviewer")
    number(rounds, "evaluation rounds", 1, 5, integral=True)
    raw = Path(corpus_path).read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise CirraError("evaluation corpus exceeds 4 MiB")
    corpus = json.loads(raw)
    cases = corpus.get("cases", [])
    if (
        corpus.get("version") != 1
        or len(cases) != 10
        or len({case["id"] for case in cases}) != 10
        or sum(bool(case["labels"]) for case in cases) != 6
        or sum(case["split"] == "holdout" for case in cases) != 2
        or sum(bool(case["labels"]) and case["split"] == "holdout" for case in cases) != 1
    ):
        raise CirraError(
            "evaluation needs ten unique cases: six defective, four clean, one of each held out"
        )
    freeze = {
        "corpus_digest": digest(corpus),
        "config_digest": config.digest,
        "protocol": PROTOCOL,
        "prompt_digest": digest(INSTRUCTIONS),
        "rounds": rounds,
        "matching": "path, side, overlapping labelled range, all root-cause concept groups",
        "targets": {
            "defects_detected": 5,
            "clean_false_findings_maximum": 1,
            "heldout_defect_every_round": True,
        },
    }
    directory = Path(state_dir) / "evaluations" / digest(freeze)
    directory.mkdir(parents=True, exist_ok=True)
    frozen = directory / "frozen.json"
    if not frozen.exists():
        frozen.write_text(canonical(freeze) + "\n", encoding="utf-8")
    elif json.loads(frozen.read_text(encoding="utf-8")) != freeze:
        raise CirraError("evaluation freeze does not match")
    results = []
    for round_number in range(1, rounds + 1):
        scored = []
        # Each round owns an independent durable store. Resume reuses responses;
        # separate rounds dispatch independent calls with the same frozen input.
        store = Store(directory / f"round-{round_number}")
        for case in cases:
            provider = FixtureProvider.from_data(case["snapshot"], config)
            report, _ = review(provider, config, store)
            export_report(report, directory / f"round-{round_number}")
            scored.append(score(case, report))
        detected = sum(item["detected"] for item in scored)
        false_clean = sum(
            len(item["spurious_findings"]) for item in scored if not item["defective"]
        )
        heldout = all(
            item["detected"] for item in scored if item["defective"] and item["split"] == "holdout"
        )
        successful = all(item["outcome"] == "succeeded" for item in scored)
        results.append(
            {
                "round": round_number,
                "detected": detected,
                "clean_false_findings": false_clean,
                "heldout_defect_detected": heldout,
                "all_reviews_succeeded": successful,
                "target_met": detected >= 5 and false_clean <= 1 and heldout and successful,
                "cases": scored,
            }
        )
        result = {
            "schema_version": 1,
            "frozen": freeze,
            "observed_at": utc_now(),
            "rounds": results,
            "adjudication": "Automated label matching only; inspect findings for human usefulness.",
            "cost_note": "API-equivalent estimates are not bills; subscription cost is unknown.",
        }
        (directory / "results.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
    return {
        "results_path": str((directory / "results.json").resolve()),
        "rounds": [
            {
                key: item[key]
                for key in (
                    "round",
                    "detected",
                    "clean_false_findings",
                    "heldout_defect_detected",
                    "target_met",
                )
            }
            for item in results
        ],
    }
