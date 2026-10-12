"""Evaluate real, version-pinned Invocations output in Foundry (not canned answers)."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import VersionRefIndicator
from azure.core.exceptions import AzureError
import httpx
from openai import APIError

from collect_eval import collect
from provenance import (artifact_identity, credential as get_credential, source_revision,
                        toolbox_fingerprint, toolbox_reference)
from quality_gate import INSTRUCTIONS, evaluator_sha256
from register_evaluator import verify_evaluator

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluations/public-library-3d1-seed-v1.jsonl"
CRITERIA = ROOT / "ci/criteria.json"
DECLARED_TOOLS = ROOT / ".agent_configs/baseline/tools.json"


POLICY_BLOCKS = ("cyber_policy", "content_filter", "ResponsibleAIPolicyViolation")
POLICY_BLOCK_REPLY = "I can't help with that request. (The model service refused this prompt under its safety policy.)"
# Same read timeout as the baseline smoke (scripts/smoke.py): a multi-tool case that passed the baseline must not time
# out here only because this limit was shorter.
INVOCATION_TIMEOUT_SECONDS = 300
# Gateway timeouts and throttling are not quality evidence: retry that case in a fresh session like a transport error.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def failure_reason(error: Exception) -> str:
    """One line saying why an agent invocation failed, so a hang, a throttle and a gateway timeout can be told apart."""
    if isinstance(error, httpx.HTTPStatusError):
        body = " ".join(error.response.text.split())[:200]
        return f"HTTP {error.response.status_code} from the agent endpoint" + (f": {body}" if body else "")
    if isinstance(error, httpx.TimeoutException):
        return f"no answer within {INVOCATION_TIMEOUT_SECONDS}s ({type(error).__name__})"
    return f"{type(error).__name__}: {' '.join(str(error).split())[:200]}"


def policy_refusal(response) -> dict | None:
    """The agent endpoint's Content Safety rejected the prompt at the input stage (HTTP 400 `content_filter`, no stream).

    That is the platform refusing an adversarial row before the agent runs: a refusal to grade, not a failed collection."""
    if response.status_code != 400 or not any(marker in response.text for marker in POLICY_BLOCKS):
        return None
    return {"text": POLICY_BLOCK_REPLY, "tool_calls": [], "tool_results": {}, "tool_definitions": None}


def parse_stream_events(body: str) -> dict:
    """Parse the Invocations SSE stream into final text plus observed tool activity."""
    chunks = []
    calls: list[dict] = []
    results: dict[str, dict] = {}
    definitions = None
    done = False
    blocked = False
    for frame in re.split(r"\r?\n\r?\n", body.strip()):
        event = ""
        data = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if not data:
            continue
        payload = json.loads("\n".join(data))
        kind = payload.get("type")
        if event in {"error", "response.failed"} or kind in {"error", "response.failed", "session.error"}:
            # An adversarial row the model service itself refuses is a refusal to grade, not a failed collection.
            if any(marker in json.dumps(payload) for marker in POLICY_BLOCKS):
                blocked = True
                continue
            raise RuntimeError(f"Agent stream failed: {payload}")
        if done and (kind or "delta" in payload):
            raise ValueError("Agent stream continued after done")
        if "delta" in payload:
            if not isinstance(payload["delta"], str):
                raise ValueError("Invalid delta in agent stream")
            chunks.append(payload["delta"])
        elif kind == "tool_definitions":
            if definitions is not None or not isinstance(payload.get("tools"), list):
                raise ValueError("Invalid tool definitions in agent stream")
            definitions = payload["tools"]
        elif kind == "tool_call":
            if not payload.get("tool_call_id") or not isinstance(payload.get("name"), str):
                raise ValueError("Invalid tool call in agent stream")
            calls.append({"type": "tool_call", "tool_call_id": payload["tool_call_id"],
                          "name": payload["name"], "arguments": payload.get("arguments") or {}})
        elif kind == "tool_result":
            if payload.get("tool_call_id") not in {call["tool_call_id"] for call in calls}:
                raise ValueError("Tool result without a matching tool call")
            results[payload["tool_call_id"]] = payload
        if event == "done":
            done = True
    text = "".join(chunks).strip()
    if blocked and not text:
        text = POLICY_BLOCK_REPLY
    if not done or not text:
        raise ValueError("Agent stream was incomplete or empty")
    return {"text": text, "tool_calls": calls, "tool_results": results,
            "tool_definitions": definitions}


def parse_stream(body: str) -> str:
    return parse_stream_events(body)["text"]


def declared_tool_definitions() -> list[dict]:
    return [tool["function"] for tool in json.loads(DECLARED_TOOLS.read_text())]


def observed_tool_definitions(observed: dict) -> tuple[list[dict], str]:
    """Tools the runtime exposed (or the declared schema for legacy runtimes), plus undeclared built-ins it called."""
    definitions = [dict(tool) for tool in observed["tool_definitions"] or []]
    source = "runtime"
    if not definitions:
        # Legacy runtimes do not report tools; tool evaluators need a schema to report not-applicable.
        definitions, source = declared_tool_definitions(), "declared"
    known = {tool.get("name") for tool in definitions}
    for call in observed["tool_calls"]:
        if call["name"] not in known:
            known.add(call["name"])
            definitions.append({"name": call["name"], "description": "Copilot SDK built-in tool (undeclared)",
                                "parameters": {"type": "object", "properties": {}}})
    return definitions, source


def conversation(observed: dict) -> list[dict]:
    """Agent turn in the Foundry agent-message format used by the agentic evaluators."""
    messages = []
    for call in observed["tool_calls"]:
        messages.append({"role": "assistant", "content": [dict(call)]})
        result = observed["tool_results"].get(call["tool_call_id"])
        if result is not None:
            output = result.get("result") if result.get("success") else f"ERROR: {result.get('error') or 'tool failed'}"
            messages.append({"role": "tool", "tool_call_id": call["tool_call_id"],
                             "content": [{"type": "tool_result", "tool_result": output}]})
    messages.append({"role": "assistant", "content": [{"type": "text", "text": observed["text"]}]})
    return messages


def evaluation_item(index: int, row: dict, observed: dict, instructions: str) -> dict:
    query_messages = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": [{"type": "text", "text": row["query"]}]},
    ]
    turn = conversation(observed)
    definitions, definitions_source = observed_tool_definitions(observed)
    return {
        "case_id": str(index), "query": row["query"], "expected_behavior": row["expected_behavior"],
        "response": observed["text"], "query_messages": query_messages, "conversation": turn,
        "tool_calls": observed["tool_calls"],
        "tool_definitions": definitions, "tool_definitions_source": definitions_source,
    }


ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        **{key: {"type": "string"} for key in ("case_id", "query", "expected_behavior", "response",
                                               "tool_definitions_source")},
        **{key: {"type": "array"} for key in ("query_messages", "conversation",
                                              "tool_calls", "tool_definitions")},
    },
    "required": ["case_id", "query", "expected_behavior", "response", "tool_definitions_source", "query_messages",
                 "conversation", "tool_calls", "tool_definitions"],
}


def invoke(project, credential, endpoint: str, agent: str, version: str, query: str,
           include_tool_events: bool = True) -> dict:
    session_id = f"ci-{uuid.uuid4().hex}"
    try:
        project.beta.agents.create_session(
            agent, version_indicator=VersionRefIndicator(agent_version=version),
            agent_session_id=session_id,
        )
        token = credential.get_token("https://ai.azure.com/.default").token
        response = httpx.post(
            f"{endpoint.rstrip('/')}/agents/{agent}/endpoint/protocols/invocations",
            params={"api-version": "v1", "agent_session_id": session_id},
            headers={"Authorization": f"Bearer {token}"},
            # memory: false - evaluation cases share one identity, so per-user memory would leak between cases.
            json={"input": query, "memory": False, **({"include_tool_events": True} if include_tool_events else {})},
            timeout=INVOCATION_TIMEOUT_SECONDS,
        )
        if refusal := policy_refusal(response):
            return refusal
        response.raise_for_status()
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise ValueError("Expected an Invocations event stream")
        return parse_stream_events(response.text)
    finally:
        project.beta.agents.delete_session(agent, session_id)


def evaluate(endpoint: str, agent: str, version: str, output: Path, timeout: int,
             concurrency: int = 4) -> dict:
    if not re.fullmatch(r"[1-9][0-9]*", version):
        raise ValueError("Evaluation requires an explicit immutable numeric agent version")
    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    queries = [row["query"] for row in rows]
    if not rows or len(queries) != len(set(queries)):
        raise ValueError("Dataset must contain unique, nonempty cases")
    criteria = json.loads(CRITERIA.read_text())
    provenance = {
        "agent": agent, "version": version, "endpoint": endpoint.rstrip("/"),
        "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "criteria_sha256": hashlib.sha256(CRITERIA.read_bytes()).hexdigest(),
        "evaluator_sha256": evaluator_sha256(),
        "instructions_sha256": hashlib.sha256(INSTRUCTIONS.read_bytes()).hexdigest(),
        "case_count": len(rows),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with get_credential() as credential, AIProjectClient(
        endpoint=endpoint, credential=credential, allow_preview=True
    ) as project, project.get_openai_client() as client:
        for criterion in criteria:
            if criterion.get("evaluator_version"):
                verify_evaluator(project, criterion["evaluator_name"], criterion["evaluator_version"])
        instructions = INSTRUCTIONS.read_text()
        provenance.update(artifact_identity(project, agent, version))
        # The toolbox is an external dependency; record exactly what guidance the agent saw.
        provenance.update(toolbox_fingerprint(
            endpoint, credential.get_token("https://ai.azure.com/.default").token, *toolbox_reference()))
        provenance["evaluation_git_sha"] = source_revision()
        provenance["policy_sha256"] = hashlib.sha256((ROOT / "ci/quality_gate.py").read_bytes()).hexdigest()
        provenance["github_run_id"] = os.environ.get("GITHUB_RUN_ID")
        provenance["case_elapsed_seconds"] = {}
        print(f"Collecting {len(rows)} real responses from {agent}:v{version}, one fresh session per case, "
              f"{concurrency} at a time", flush=True)

        def run_case(index: int, row: dict):
            start = time.monotonic()
            for attempt in range(3):
                try:
                    return index, invoke(project, credential, endpoint, agent, version, row["query"]), None, \
                        time.monotonic() - start
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    if attempt == 2 or (status is not None and status not in RETRYABLE_STATUS):
                        return index, None, exc, time.monotonic() - start
                    reason = f"HTTP {status}" if status else f"transport error ({type(exc).__name__})"
                    print(f"{agent}:{version} case {index + 1}: {reason}; retrying in a fresh session", flush=True)
                    time.sleep(10 * (attempt + 1))
                except (AzureError, httpx.HTTPError, RuntimeError, ValueError) as exc:
                    return index, None, exc, time.monotonic() - start

        observed_by_case: dict[int, dict] = {}
        failures: list[tuple[int, Exception]] = []
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for index, observed, error, elapsed in pool.map(lambda pair: run_case(*pair), enumerate(rows)):
                provenance["case_elapsed_seconds"][str(index)] = round(elapsed, 3)
                if error is not None:
                    failures.append((index, error))
                    print(f"{agent}:{version} case {index + 1}/{len(rows)}: FAILED after {elapsed:.1f}s: "
                          f"{failure_reason(error)}", flush=True)
                    continue
                observed_by_case[index] = observed
                tools = ", ".join(call["name"] for call in observed["tool_calls"]) or "no tool call"
                print(f"{agent}:{version} case {index + 1}/{len(rows)}: {elapsed:.1f}s ({tools})", flush=True)
        if failures:
            index, error = failures[0]
            output.write_text(json.dumps({
                "provenance": provenance,
                "failure_kind": "agent_execution",
                "run": {"status": "failed", "error": {"case_id": str(index), "message": str(error),
                                                      "failed_cases": [str(i) for i, _ in failures]}},
                "output_items": [],
            }, indent=2) + "\n")
            raise error
        content = [{"item": evaluation_item(index, row, observed_by_case[index], instructions)}
                   for index, row in enumerate(rows)]
        samples_path = output.with_suffix(".samples.json")
        samples_path.write_text(json.dumps(content, indent=2) + "\n")
        try:
            definition = client.evals.create(
                name=f"{agent}-agent-quality",
                data_source_config={"type": "custom", "include_sample_schema": False,
                                    "item_schema": ITEM_SCHEMA},
                testing_criteria=[], extra_body={"testing_criteria": criteria},
            )
            run = client.evals.runs.create(
                definition.id, name=f"{agent}-v{version}",
                data_source={"type": "jsonl", "source": {"type": "file_content", "content": content}},
                metadata={"agent": agent, "agent_version": version},
            )
        except APIError as exc:
            status = getattr(exc, "status_code", None)
            kind = "evaluation_authentication" if status in {401, 403} else "evaluation_service"
            output.write_text(json.dumps({
                "provenance": provenance, "failure_kind": kind,
                "run": {"status": "failed", "error": {
                    "code": kind, "http_status": status,
                    "request_id": getattr(exc, "request_id", None),
                }}, "output_items": [],
            }, indent=2) + "\n")
            raise RuntimeError(f"Scoring submission failed ({kind}); not an agent-quality verdict. Inspect {output}") from exc
        pending = {**provenance, "eval_id": definition.id, "run_id": run.id}
        output.with_suffix(".pending.json").write_text(json.dumps(pending, indent=2) + "\n")
        print(f"Foundry eval {definition.id}/{run.id}", flush=True)
        if getattr(run, "report_url", None):
            print(f"Open scoring progress in Foundry: {run.report_url}", flush=True)
    report = collect(endpoint, definition.id, run.id, output, timeout, provenance=provenance,
                     normalize=True)
    output.with_suffix(".pending.json").unlink()
    passed = sum(all(result["passed"] is not False for result in item["results"])
                 for item in report["output_items"])
    print(f"Scoring complete for {agent}:v{version}: {passed}/{len(rows)} cases passed every applicable grader. "
          "Release eligibility is determined separately by the comparison gate.", flush=True)
    if report["run"].get("report_url"):
        print(f"Foundry result: {report['run']['report_url']}", flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--agent", default="public-library-3d1")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("CI_CASE_CONCURRENCY", "4")),
                        help="Cases invoked in parallel (each in its own fresh session)")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency must be between 1 and 8")
    evaluate(args.endpoint, args.agent, args.version, args.output, args.timeout, args.concurrency)
