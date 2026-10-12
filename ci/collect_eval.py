"""Download terminal Foundry evaluation metadata AND every scored output item."""

import argparse
import json
import math
from pathlib import Path
import time

from azure.ai.projects import AIProjectClient
from provenance import credential as get_credential
from quality_gate import FOUNDRY_METRICS


class EvaluationInfrastructureError(RuntimeError):
    pass


def failure_kind(run: dict) -> str:
    error = run.get("error") or {}
    detail = json.dumps(error).lower()
    if any(value in detail for value in ("permissiondenied", "unauthorized", "401", "403")):
        return "evaluation_authentication"
    return "evaluation_service"


def _normalize_metric(name: str, results: list[dict], spec: dict) -> dict:
    if len(results) > 2:
        raise ValueError(f"Ambiguous native metric: {name}")
    if all(r.get("status") == "skipped" and r.get("label") == "not_applicable" for r in results):
        if not spec["optional"] or any(r.get("score") is not None for r in results):
            raise ValueError(f"Required native metric was skipped: {name}")
        return {"name": name, "score": None, "passed": None, "applicable": False,
                "label": "not_applicable", "status": "skipped", "reason": results[0].get("reason")}
    scored = [r for r in results if type(r.get("score")) in {int, float} and math.isfinite(r["score"])]
    decided = [r for r in results if type(r.get("passed")) is bool]
    if len(scored) != 1 or len(decided) != 1:
        raise ValueError(f"Ambiguous or unscored native metric: {name}")
    score = scored[0]["score"]
    low, high = spec["range"]
    if not low <= score <= high or (
        spec["kind"] == "binary" and (score not in (0, 1) or decided[0]["passed"] != bool(score))
    ):
        raise ValueError(f"Invalid native score or pass label: {name}")
    if any(r.get("status", "completed") != "completed" or (r.get("sample") or {}).get("error")
           for r in results):
        raise ValueError(f"Native metric did not complete: {name}")
    return {**decided[0], "name": name, "score": score, "passed": decided[0]["passed"],
            "applicable": True}


def normalize_grades(items: list[dict], metrics: dict | None = None) -> list[dict]:
    metrics = FOUNDRY_METRICS if metrics is None else metrics
    normalized = []
    for item in items:
        raw_results = item["results"]
        groups: dict[str, list[dict]] = {}
        for result in raw_results:
            if result["name"] not in metrics or result.get("error"):
                raise ValueError("Unknown evaluator or failed grader in native result")
            groups.setdefault(result["name"], []).append(result)
        if set(groups) != set(metrics):
            raise ValueError("Native evaluator output is missing a required metric")
        grades = [_normalize_metric(name, results, metrics[name]) for name, results in groups.items()]
        normalized.append({**item, "raw_results": raw_results, "results": grades})
    return normalized


def collect(endpoint: str, eval_id: str, run_id: str, output: Path, timeout: int,
            provenance: dict | None = None, expected_items: int | None = None,
            normalize: bool = False, metrics: dict | None = None) -> dict:
    if expected_items is None and provenance is not None:
        expected_items = provenance.get("case_count")
    if expected_items is not None and (type(expected_items) is not int or expected_items < 1):
        raise ValueError("Expected item count must be a positive integer")
    output.parent.mkdir(parents=True, exist_ok=True)

    def save(report):
        if provenance is not None:
            report["provenance"] = provenance
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    with get_credential() as credential, AIProjectClient(
        endpoint=endpoint, credential=credential, allow_preview=True
    ) as project, project.get_openai_client() as client:
        deadline = time.monotonic() + timeout
        while True:
            run = client.evals.runs.retrieve(run_id, eval_id=eval_id)
            if run.status in {"completed", "failed", "canceled", "cancelled"}:
                break
            if time.monotonic() >= deadline:
                save({"run": run.model_dump(mode="json", warnings=False), "output_items": [],
                      "failure_kind": "evaluation_timeout"})
                raise TimeoutError(f"Evaluation {eval_id}/{run_id} is still {run.status}")
            time.sleep(15)
        report = {
            "definition": client.evals.retrieve(eval_id).model_dump(mode="json", warnings=False),
            "run": run.model_dump(mode="json", warnings=False),
            "output_items": [
                item.model_dump(mode="json", warnings=False)
                for item in client.evals.runs.output_items.list(run_id, eval_id=eval_id)
            ],
        }
        if run.status == "completed" and expected_items is not None:
            visibility_deadline = min(deadline, time.monotonic() + 60)
            while len(report["output_items"]) < expected_items and time.monotonic() < visibility_deadline:
                print(f"Waiting for result visibility: {len(report['output_items'])}/{expected_items}", flush=True)
                time.sleep(5)
                report["output_items"] = [
                    item.model_dump(mode="json", warnings=False)
                    for item in client.evals.runs.output_items.list(run_id, eval_id=eval_id)
                ]
        if run.status != "completed" or report["run"].get("error"):
            report["failure_kind"] = failure_kind(report["run"])
            save(report)
            raise EvaluationInfrastructureError(
                f"Evaluation failed ({report['failure_kind']}); this is not an agent-quality score. Inspect {output}"
            )
        if not report["output_items"] or (expected_items is not None and len(report["output_items"]) != expected_items):
            report["failure_kind"] = "incomplete_evaluation"
            save(report)
            raise EvaluationInfrastructureError(f"Incomplete evaluation output; inspect {output}")
        if normalize:
            try:
                report["output_items"] = normalize_grades(report["output_items"], metrics)
            except ValueError:
                report["failure_kind"] = "evaluation_contract"
                save(report)
                raise
    save(report)
    print(f"{eval_id}/{run_id}: {run.status}; {len(report['output_items'])} items -> {output}")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--eval-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    collect(args.endpoint, args.eval_id, args.run_id, args.output, args.timeout)
