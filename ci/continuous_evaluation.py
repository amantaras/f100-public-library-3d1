"""Continuous evaluation of the production hosted agent from its recent Foundry traces.

Hosted agents do not raise the per-response event that Foundry evaluation rules use for
prompt agents; Microsoft's hosted-agent pattern is a recurring evaluation over the agent's
traces in Application Insights. This script runs one such evaluation over a rolling window
(recomputed on every run) into a stable Foundry evaluation group, so results accumulate in
the Foundry portal as a time series. It reports scores; it does not gate releases.
"""

import argparse
import json
import os
from pathlib import Path
import time

from azure.ai.projects import AIProjectClient
from openai import APIError

from provenance import credential as get_credential

EVAL_NAME = "f100-public-library-3d1-production-continuous"
EVALUATORS = ("relevance", "intent_resolution", "task_completion", "task_adherence", "indirect_attack")
NO_EVIDENCE = ("no trace", "none contained", "no spans", "found 0")


def criteria(deployment: str) -> list[dict]:
    return [{
        "type": "azure_ai_evaluator", "name": name, "evaluator_name": f"builtin.{name}",
        "initialization_parameters": {} if name == "indirect_attack" else {"deployment_name": deployment},
        "data_mapping": {"query": "{{item.query}}", "response": "{{item.response}}"},
    } for name in EVALUATORS]


def eval_group(client, deployment: str) -> str:
    for existing in client.evals.list(limit=100):
        if existing.name == EVAL_NAME:
            return existing.id
    created = client.evals.create(
        name=EVAL_NAME, data_source_config={"type": "azure_ai_source", "scenario": "traces"},
        testing_criteria=[], extra_body={"testing_criteria": criteria(deployment)},
        metadata={"purpose": "continuous-evaluation", "environment": "production"},
    )
    return created.id


def summarize(items: list[dict]) -> dict:
    metrics: dict[str, dict] = {}
    for item in items:
        for result in item.get("results", []):
            entry = metrics.setdefault(result["name"], {"scores": [], "passed": 0, "failed": 0, "not_applicable": 0})
            if result.get("score") is None:
                entry["not_applicable"] += 1
                continue
            entry["scores"].append(result["score"])
            entry["passed" if result.get("passed") else "failed"] += 1
    return {name: {"mean": sum(v["scores"]) / len(v["scores"]) if v["scores"] else None,
                   "passed": v["passed"], "failed": v["failed"], "not_applicable": v["not_applicable"]}
            for name, v in metrics.items()}


def run(endpoint: str, agent: str, deployment: str, hours: int, max_traces: int, timeout: int) -> dict:
    with get_credential() as credential, AIProjectClient(
        endpoint=endpoint, credential=credential, allow_preview=True
    ) as project, project.get_openai_client() as client:
        eval_id = eval_group(client, deployment)
        now = int(time.time())
        data_source = {"type": "azure_ai_trace_data_source_preview", "trace_source": {
            "type": "agent_filter", "agent_name": agent, "start_time": now - hours * 3600,
            "end_time": now + 600, "max_traces": max_traces}}
        started = client.evals.runs.create(eval_id, name=f"continuous-{time.strftime('%Y%m%d-%H%M', time.gmtime())}",
                                           data_source=data_source,
                                           metadata={"window_hours": str(hours), "environment": "production"})
        deadline = time.monotonic() + timeout
        current = started
        while current.status not in {"completed", "failed", "canceled", "cancelled"}:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Continuous evaluation {eval_id}/{started.id} still {current.status}")
            time.sleep(15)
            current = client.evals.runs.retrieve(started.id, eval_id=eval_id)
        record = {"eval_id": eval_id, "run_id": started.id, "status": current.status,
                  "window_hours": hours, "report_url": getattr(current, "report_url", None)}
        error = current.error.model_dump() if getattr(current, "error", None) else None
        if current.status != "completed":
            message = json.dumps(error or {}).lower()
            record["status"] = "no_traces" if any(token in message for token in NO_EVIDENCE) else current.status
            record["error"] = error
            return record
        items = [item.model_dump(mode="json") for item in client.evals.runs.output_items.list(started.id, eval_id=eval_id)]
        record.update(traces=len(items), metrics=summarize(items))
        return record


def markdown(record: dict) -> str:
    lines = ["## Continuous evaluation (production traces)",
             f"- **Window:** last {record['window_hours']} h · **status:** `{record['status']}`"]
    if record.get("report_url"):
        lines.append(f"- [Open in Foundry]({record['report_url']})")
    if record["status"] == "no_traces":
        lines.append("- No production traces with message content in this window; nothing was scored.")
    for name, metric in (record.get("metrics") or {}).items():
        mean = "N/A" if metric["mean"] is None else f"{metric['mean']:.2f}"
        lines.append(f"- **{name}:** mean {mean} · pass {metric['passed']} / fail {metric['failed']}"
                     f" · N/A {metric['not_applicable']}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--agent", default="public-library-3d1")
    parser.add_argument("--deployment", default="gpt-6-luna")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--max-traces", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.endpoint, args.agent, args.deployment, args.hours, args.max_traces, args.timeout)
    except APIError as exc:
        result = {"status": "scoring_error", "window_hours": args.hours, "error": str(exc)[:500]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    text = markdown(result)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(text)
    raise SystemExit(0 if result["status"] in {"completed", "no_traces"} else 1)
