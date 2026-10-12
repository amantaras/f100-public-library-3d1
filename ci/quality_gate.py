"""Require complete, comparable evaluations and no regression from the baseline."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluations/public-library-3d1-seed-v1.jsonl"
CRITERIA = ROOT / "ci/criteria.json"
EVALUATOR = ROOT / "ci/expected_behavior_evaluator.json"
RUBRIC_EVALUATOR = ROOT / "ci/domain_rubric_evaluator.json"
INSTRUCTIONS = ROOT / ".agent_configs/baseline/instructions.md"
PINNED_EVALUATORS = (EVALUATOR, RUBRIC_EVALUATOR)

# kind: binary scores are 0/1 and must agree with pass/fail.
# gate: "case" blocks any per-case drop; "aggregate" blocks a paired mean or
#       pass-rate drop larger than one case's worth of judge disagreement.
# optional: the evaluator may report not_applicable (for example no tool call).
# source: "foundry" is scored by a native Foundry evaluator; "deterministic" is
#         recomputed here from the observed tool calls and the dataset.
METRICS = {
    "relevance": {"range": (1, 5), "kind": "ordinal", "gate": "case", "optional": False, "source": "foundry"},
    "expected_behavior": {"range": (0, 1), "kind": "binary", "gate": "case", "optional": False, "source": "foundry"},
    "indirect_attack": {"range": (0, 1), "kind": "binary", "gate": "case", "optional": False, "source": "foundry"},
    "task_adherence": {"range": (0, 1), "kind": "binary", "gate": "aggregate", "optional": False, "source": "foundry"},
    "task_completion": {"range": (0, 1), "kind": "binary", "gate": "aggregate", "optional": False, "source": "foundry"},
    "intent_resolution": {"range": (1, 5), "kind": "ordinal", "gate": "aggregate", "optional": False, "source": "foundry"},
    "domain_rubric": {"range": (0, 1), "kind": "continuous", "gate": "aggregate", "optional": False, "source": "foundry"},
    "tool_call_accuracy": {"range": (1, 5), "kind": "ordinal", "gate": "aggregate", "optional": True, "source": "foundry"},
    "tool_selection": {"range": (0, 1), "kind": "continuous", "gate": "aggregate", "optional": True, "source": "foundry"},
    "tool_input_accuracy": {"range": (0, 1), "kind": "binary", "gate": "aggregate", "optional": True, "source": "foundry"},
    "tool_output_utilization": {"range": (0, 1), "kind": "continuous", "gate": "aggregate", "optional": True, "source": "foundry"},
    "tool_call_success": {"range": (0, 1), "kind": "continuous", "gate": "aggregate", "optional": True, "source": "foundry"},
    "tool_route_match": {"range": (0, 1), "kind": "binary", "gate": "aggregate", "optional": False, "source": "deterministic"},
}
FOUNDRY_METRICS = {name: spec for name, spec in METRICS.items() if spec["source"] == "foundry"}
EPSILON = 1e-9


def evaluator_sha256() -> str:
    digest = hashlib.sha256()
    for path in PINNED_EVALUATORS:
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def load_rows() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def expected_tool_names(row: dict) -> set[str]:
    return {call["name"] for call in row.get("tool_calls") or []}


def observed_tool_names(source: dict) -> set[str]:
    calls = source.get("tool_calls")
    if not isinstance(calls, list):
        raise ValueError("Observed tool calls are missing from the evaluated item")
    names = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "tool_call" or not isinstance(call.get("name"), str):
            raise ValueError("Malformed observed tool call")
        names.add(call["name"])
    return names


def tool_route_match(row: dict, source: dict) -> float:
    """1 when every expected tool was actually called (no expected tool: no tool call)."""
    expected, observed = expected_tool_names(row), observed_tool_names(source)
    if not expected:
        return float(not observed)
    return float(expected <= observed)


def check_result(name: str, result: dict, case_id: str) -> tuple[float | None, bool | None]:
    spec = METRICS[name]
    if result.get("error") or (result.get("sample") or {}).get("error"):
        raise ValueError(f"Evaluator error for {name} on case {case_id}")
    if result.get("applicable") is False:
        if not spec["optional"] or result.get("score") is not None or result.get("passed") is not None:
            raise ValueError(f"{name} cannot be not-applicable for case {case_id}")
        return None, None
    score, passed = result.get("score"), result.get("passed")
    low, high = spec["range"]
    if (type(passed) is not bool or type(score) not in {int, float} or not math.isfinite(score)
            or not low <= score <= high
            or (spec["kind"] == "binary" and (score not in (0, 1) or passed != bool(score)))
            or result.get("status", "completed") != "completed"):
        raise ValueError(f"Invalid/unscored {name} result for case {case_id}")
    return score, passed


def validate(report: dict) -> dict:
    rows = load_rows()
    provenance = report["provenance"]
    for field, digest in [("dataset_sha256", hashlib.sha256(DATASET.read_bytes()).hexdigest()),
                          ("criteria_sha256", hashlib.sha256(CRITERIA.read_bytes()).hexdigest()),
                          ("evaluator_sha256", evaluator_sha256()),
                          ("instructions_sha256", hashlib.sha256(INSTRUCTIONS.read_bytes()).hexdigest())]:
        if provenance[field] != digest:
            raise ValueError(f"{field} does not match the checked-in evaluation suite")
    if provenance["policy_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("Evaluation used a different release policy")
    if report["run"]["status"] != "completed" or report["run"].get("error"):
        raise ValueError("Evaluation did not complete successfully")
    if not report["run"].get("id") or not report["run"].get("eval_id"):
        raise ValueError("Missing Foundry evaluation identity")
    if provenance["case_count"] != len(rows) or len(report["output_items"]) != len(rows):
        raise ValueError("Incomplete evaluation coverage")
    scores = {}
    seen = set()
    for item in report["output_items"]:
        if item["status"] not in {"completed", "pass", "fail"}:
            raise ValueError(f"Unscored output item: {item['status']}")
        if (item.get("sample") or {}).get("error"):
            raise ValueError("Agent sample has an error")
        if item["eval_id"] != report["run"]["eval_id"] or item["run_id"] != report["run"]["id"]:
            raise ValueError("Output item belongs to another evaluation run")
        source = item["datasource_item"]
        if "item" in source:
            source = source["item"]
        case_id = source["case_id"]
        if case_id in seen or case_id not in {str(i) for i in range(len(rows))}:
            raise ValueError(f"Duplicate or unknown case: {case_id}")
        seen.add(case_id)
        row = rows[int(case_id)]
        if any(source[key] != row[key] for key in ("query", "expected_behavior")):
            raise ValueError(f"Case {case_id} does not match the dataset")
        if not isinstance(source["response"], str) or not source["response"].strip():
            raise ValueError("Missing real agent response")
        if not isinstance(source.get("tool_definitions"), list):
            raise ValueError("Missing observed tool definitions")
        if source.get("tool_definitions_source") not in {"runtime", "declared"}:
            raise ValueError("Unknown tool definition source")
        results = item["results"]
        names = [r["name"] for r in results]
        if len(names) != len(FOUNDRY_METRICS) or set(names) != set(FOUNDRY_METRICS):
            raise ValueError("Missing, unexpected, or duplicate evaluator results")
        for result in results:
            scores[(case_id, result["name"])] = check_result(result["name"], result, case_id)
        route = tool_route_match(row, source)
        scores[(case_id, "tool_route_match")] = (route, bool(route))
    return scores


def _paired(before: dict, after: dict, name: str) -> list[tuple]:
    return [(before[key], after[key]) for key in sorted(before)
            if key[1] == name and before[key][0] is not None and after[key][0] is not None]


def compare(baseline: dict, candidate: dict) -> dict:
    for key in ("agent", "endpoint", "dataset_sha256", "criteria_sha256", "evaluator_sha256",
                "instructions_sha256", "policy_sha256", "case_count"):
        if baseline["provenance"][key] != candidate["provenance"][key]:
            raise ValueError(f"Incomparable evaluation {key}")
    before, after = validate(baseline), validate(candidate)
    failures, watch = [], []
    metrics = {}
    for name, spec in METRICS.items():
        pairs = _paired(before, after, name)
        applicable = lambda scores: sum(value[0] is not None for key, value in scores.items() if key[1] == name)
        entry = {
            "gate": spec["gate"], "kind": spec["kind"], "range": list(spec["range"]),
            "optional": spec["optional"], "source": spec["source"], "paired_cases": len(pairs),
            "baseline_applicable": applicable(before), "candidate_applicable": applicable(after),
            "baseline_mean": None, "candidate_mean": None,
            "baseline_pass_rate": None, "candidate_pass_rate": None,
        }
        if not pairs:
            entry["status"] = "not_comparable"
            metrics[name] = entry
            continue
        count = len(pairs)
        entry.update(
            baseline_mean=sum(old[0] for old, _ in pairs) / count,
            candidate_mean=sum(new[0] for _, new in pairs) / count,
            baseline_pass_rate=sum(old[1] for old, _ in pairs) / count,
            candidate_pass_rate=sum(new[1] for _, new in pairs) / count,
        )
        low, high = spec["range"]
        tolerance_mean = 0.0 if spec["gate"] == "case" else (high - low) / count
        tolerance_rate = 0.0 if spec["gate"] == "case" else 1 / count
        entry.update(tolerance_mean=tolerance_mean, tolerance_rate=tolerance_rate)
        regressed = (entry["candidate_mean"] < entry["baseline_mean"] - tolerance_mean - EPSILON
                     or entry["candidate_pass_rate"] < entry["baseline_pass_rate"] - tolerance_rate - EPSILON)
        entry["status"] = "regression" if regressed else "pass"
        if regressed:
            failures.append(f"{name}: aggregate regression")
        metrics[name] = entry
    for key, (old_score, old_pass) in sorted(before.items()):
        new_score, new_pass = after[key]
        if old_score is None or new_score is None:
            continue
        if new_score < old_score or (old_pass and not new_pass):
            message = f"case {key[0]} {key[1]}: regression"
            (failures if METRICS[key[1]]["gate"] == "case" else watch).append(message)
    return {
        "passed": not failures,
        "decision": "quality_regression" if failures else "passed",
        "policy": "baseline_non_regression",
        "failures": failures,
        "watch": watch,
        "metrics": metrics,
        "candidate_failed_checks": sum(passed is False for _, passed in after.values()),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compare(json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text()))
    except (KeyError, TypeError, ValueError) as exc:
        result = {"passed": False, "decision": "evaluation_error",
                  "policy": "baseline_non_regression", "failures": [str(exc)], "metrics": {}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
