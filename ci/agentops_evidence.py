"""Import validated Foundry results into AgentOps, preserving the original gate decision."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from quality_gate import compare, load_rows, validate, METRICS
from evaluation_report import case_comparisons, item_grades


def convert(report: dict, decision: dict) -> dict:
    validate(report)
    dataset = load_rows()
    rows = []
    passed_count = 0
    applicable: dict[str, list[float]] = {name: [] for name in METRICS}
    for item in report["output_items"]:
        source = item["datasource_item"]
        source = source.get("item", source)
        grades = item_grades(item, dataset[int(source["case_id"])])
        passed_count += all(grade["passed"] is not False for grade in grades.values())
        for name, grade in grades.items():
            if grade["score"] is not None:
                applicable[name].append(grade["score"])
        rows.append({
            "row_index": int(source["case_id"]),
            "input": source["query"], "expected": source["expected_behavior"],
            "response": source["response"],
            "metrics": [
                {"name": name, "value": grade["score"],
                 "reason": grade.get("reason") if grade["score"] is not None else "not applicable: no observed tool call"}
                for name, grade in grades.items()
            ],
        })
    started = datetime.fromtimestamp(report["run"]["created_at"], timezone.utc).isoformat()
    aggregate = {name: sum(values) / len(values) for name, values in applicable.items() if values}
    aggregate["release_policy"] = float(decision["passed"])
    thresholds = [
        {"metric": "release_policy", "criteria": "complete evidence; no baseline regression", "expected": "true",
         "actual": str(decision["passed"]).lower(), "passed": decision["passed"]}
    ]
    return {
        "version": 1, "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": max(0, report["run"].get("modified_at", report["run"]["created_at"])
                                - report["run"]["created_at"]),
        "target": {"kind": "foundry_hosted", "raw": f"{report['provenance']['agent']}:{report['provenance']['version']}",
                   "protocol": "invocations", "name": report["provenance"]["agent"],
                   "version": report["provenance"]["version"]},
        "dataset_path": "evaluations/public-library-3d1-seed-v1.jsonl",
        "evaluators": list(METRICS), "rows": rows, "aggregate_metrics": aggregate,
        "thresholds": thresholds,
        "summary": {
            "items_total": len(rows), "items_passed_all": passed_count,
            "items_pass_rate": passed_count / len(rows), "thresholds_total": 1,
            "thresholds_passed": int(decision["passed"]),
            "threshold_pass_rate": float(decision["passed"]), "overall_passed": decision["passed"],
        },
        "config": {
            "engine": "foundry-custom-invocations-adapter",
            "provenance": report["provenance"], "gate": decision,
            "eval_id": report["run"]["eval_id"], "eval_run_id": report["run"]["id"],
        },
    }


def export(baseline_path: Path, candidate_path: Path):
    from agentops.core.results import RunResult

    baseline = json.loads(baseline_path.read_text())
    candidate = json.loads(candidate_path.read_text())
    decision = compare(baseline, candidate)
    comparison = {
        "baseline_path": str(baseline_path),
        "baseline_started_at": datetime.fromtimestamp(baseline["run"]["created_at"], timezone.utc).isoformat(),
        "baseline_overall_passed": compare(baseline, baseline)["passed"],
        "metrics": [
            {"metric": name, "current": values["candidate_mean"], "baseline": values["baseline_mean"],
             "delta": values["candidate_mean"] - values["baseline_mean"],
             "direction": "regressed" if values["status"] == "regression"
             else "improved" if values["candidate_mean"] > values["baseline_mean"] else "unchanged"}
            for name, values in decision["metrics"].items() if values["status"] != "not_comparable"
        ],
        "rows": [
            {"row_index": int(case["id"]), "current_passed": case["candidate_passed"],
             "baseline_passed": case["baseline_passed"], "direction": case["change"]}
            for case in case_comparisons(baseline, candidate)
        ],
    }
    for report, gate, directory in (
        (baseline, compare(baseline, baseline), Path(".agentops/baseline")),
        (candidate, decision, Path(".agentops/results") / candidate["run"]["id"]),
        (candidate, decision, Path(".agentops/results/latest")),
    ):
        converted = convert(report, gate)
        if report is candidate:
            converted["comparison"] = comparison
        result = RunResult.model_validate(converted)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "results.json").write_text(result.model_dump_json(indent=2) + "\n")
    print(f"Imported actual Foundry evidence; release gate passed={decision['passed']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    export(args.baseline, args.candidate)
