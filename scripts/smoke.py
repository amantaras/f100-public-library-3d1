"""Smoke test and baseline for a Foundry Agent Factory use case.

    python scripts/smoke.py --url http://127.0.0.1:8089/invocations        # local run
    python scripts/smoke.py --agent f100-<id>                               # deployed (DEV)
    python scripts/smoke.py --agent f100-<id> --dataset evaluations/<id>-seed-v1.jsonl
    python scripts/smoke.py --agent f100-<id> --ask "your question"          # one ad-hoc question: tools + answer

Passes only on a completed stream (done event), a non-empty answer, no error event and the
expected tool call. HTTP 200 alone is not success.
"""
import argparse
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import uuid

import httpx
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Session create/delete can block while a slow agent (an orchestrator waiting on specialists) is still busy.
SESSION_READ_TIMEOUT = 300


def load_env() -> None:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def tool_name(spec: dict, skill: str) -> str:
    return f"f100_{spec['id']}_{skill}".replace("-", "_")


def parse_stream(body: str) -> dict:
    chunks, calls, done, errors, memory = [], [], False, [], None
    for frame in re.split(r"\r?\n\r?\n", body.strip()):
        event, data = "", []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if event == "done":
            done = True
        if not data:
            continue
        try:
            payload = json.loads("\n".join(data))
        except json.JSONDecodeError:
            continue
        kind = payload.get("type") if isinstance(payload, dict) else None
        if event in {"error", "response.failed"} or kind in {"error", "response.failed", "session.error"}:
            errors.append(payload)
        elif isinstance(payload, dict) and isinstance(payload.get("delta"), str):
            chunks.append(payload["delta"])
        elif kind == "tool_call":
            calls.append(payload.get("name"))
        elif kind == "memory":
            memory = payload
    return {"text": "".join(chunks).strip(), "tool_calls": calls, "done": done, "errors": errors, "memory": memory}


def token() -> str:
    return subprocess.check_output(
        ["az", "account", "get-access-token", "--resource", "https://ai.azure.com", "--query", "accessToken", "-o", "tsv"],
        text=True,
    ).strip()


def invoke_local(url: str, query: str, memory: bool = False) -> dict:
    response = httpx.post(url, json={"input": query, "include_tool_events": True, "memory": memory,
                                     "session_id": f"smoke-{uuid.uuid4().hex}"}, timeout=240)
    return {"status": response.status_code, **parse_stream(response.text)}


def latest_version(endpoint: str, agent: str, bearer: str) -> str:
    response = httpx.get(f"{endpoint}/agents/{agent}", params={"api-version": "v1"},
                         headers={"Authorization": f"Bearer {bearer}"}, timeout=30)
    response.raise_for_status()
    return response.json()["versions"]["latest"]["version"]


def invoke_deployed(endpoint: str, agent: str, version: str, query: str, memory: bool = False,
                    session_id: str | None = None) -> dict:
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import VersionRefIndicator
    from azure.identity import AzureCliCredential

    session_id = session_id or f"f100-smoke-{uuid.uuid4().hex}"
    credential = AzureCliCredential()
    with AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True,
                         connection_timeout=30, read_timeout=SESSION_READ_TIMEOUT) as project:
        project.beta.agents.create_session(agent, version_indicator=VersionRefIndicator(agent_version=version),
                                           agent_session_id=session_id)
        response_received = False
        try:
            response = httpx.post(
                f"{endpoint}/agents/{agent}/endpoint/protocols/invocations",
                params={"api-version": "v1", "agent_session_id": session_id},
                headers={"Authorization": f"Bearer {credential.get_token('https://ai.azure.com/.default').token}"},
                json={"input": query, "include_tool_events": True, "memory": memory},
                timeout=300,
            )
            response_received = True
            result = {"status": response.status_code, **parse_stream(response.text)}
            if response.status_code == 400 and any(marker in response.text for marker in POLICY_BLOCKS):
                # Content Safety rejects the prompt at the input stage with a JSON 400, not a stream.
                result["errors"] = [{"type": "error", "message": " ".join(response.text.split())[:300]}]
            return result
        finally:
            # A timed-out invocation can leave the session busy. Synchronous deletion then waits for
            # another full SDK read timeout and defeats the per-case hard limit. Foundry expires these
            # abandoned smoke sessions; clean up only after the invocation itself returned.
            if response_received:
                project.beta.agents.delete_session(agent, session_id)


class CaseTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise CaseTimeout("case exceeded the hard time limit")


def run_with_limit(func, seconds: int):
    """Hard per-case time limit: SIGALRM on POSIX, a watchdog thread on Windows."""
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(seconds)
        try:
            return func()
        finally:
            signal.alarm(0)
    import concurrent.futures

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(func)
    try:
        return future.result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        raise CaseTimeout("case exceeded the hard time limit") from None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def check(result: dict, expected_tools: list[str], forbid_tools: bool = False) -> list[str]:
    problems = []
    if result["status"] != 200:
        problems.append(f"HTTP {result['status']}")
    if result["errors"]:
        problems.append(f"error event: {str(result['errors'][0])[:300]}")
    if not result["done"]:
        problems.append("stream did not complete (no done event)")
    if not result["text"]:
        problems.append("empty answer")
    missing = [tool for tool in expected_tools if tool not in result["tool_calls"]]
    if missing:
        problems.append(f"expected tool(s) not called: {missing}; called: {result['tool_calls']}")
    if forbid_tools and result["tool_calls"]:
        problems.append(f"expected no tool calls; called: {result['tool_calls']}")
    return problems


POLICY_BLOCKS = ("cyber_policy", "content_filter", "ResponsibleAIPolicyViolation")
BLOCKED_ANSWER = "[blocked by the model's safety policy]"
POLICY_SAFETY = "policy_safety"


def policy_blocked(result: dict) -> bool:
    """The model service itself refused the prompt (for example `cyber_policy`), with no answer text."""
    return bool(result["errors"]) and not result["text"] and all(
        any(marker in json.dumps(error) for marker in POLICY_BLOCKS) for error in result["errors"])


def as_refusal(result: dict) -> dict:
    return {**result, "status": 200, "errors": [], "done": True, "text": BLOCKED_ANSWER, "blocked": True}


def should_retry(result: dict, problems: list[str], dataset: bool) -> bool:
    """Retries cover role/toolbox propagation and transport failures, which clear up with time.

    In a dataset run a completed answer that simply did not call the expected tool is a routing measurement, not a
    fault: refusal and prompt-injection rows never call tools, and repeating them only burned six minutes per row."""
    if not problems:
        return False
    answered = result["status"] == 200 and result["done"] and bool(result["text"]) and not result["errors"]
    return not (dataset and answered)


def retry_detail(result: dict, problems: list[str]) -> str:
    detail = problems[0]
    if result["status"] == 0 and result["errors"]:
        detail += f": {str(result['errors'][0])[:160]}"
    return detail


# A container that misses readiness this many times in a row is crashing at startup; waiting will not fix it.
NOT_READY_LIMIT = 3


def not_ready(result: dict) -> bool:
    return any("session_not_ready" in str(error) for error in result["errors"])


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="local invocations URL")
    target.add_argument("--agent", help="deployed agent name")
    parser.add_argument("--version", help="agent version (default: latest registered)")
    parser.add_argument("--dataset", help="run every dataset row (baseline) instead of the spec smoke query")
    parser.add_argument("--ask", help="send one ad-hoc question and print the tools called and the answer")
    parser.add_argument("--retries", type=int, default=1, help="attempts per case (role propagation)")
    parser.add_argument("--case-timeout", type=int, default=360, help="hard limit per attempt in seconds")
    args = parser.parse_args()

    spec = yaml.safe_load((ROOT / "usecase.yaml").read_text())
    if args.ask:
        cases = [(args.ask, [], "ask")]
    elif args.dataset:
        rows = [json.loads(line) for line in (ROOT / args.dataset).read_text().splitlines() if line.strip()]
        cases = [(row["query"], [call["name"] for call in row.get("tool_calls", [])], row.get("category")) for row in rows]
    else:
        cases = [(spec["smoke"]["query"], [tool_name(spec, spec["smoke"]["expected_skill"])], "smoke")]

    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    version = None
    if args.agent:
        version = args.version or latest_version(endpoint, args.agent, token())
        print(f"target: {args.agent} v{version} on {endpoint}")

    results = []
    for index, (query, expected, category) in enumerate(cases):
        case_started = time.monotonic()
        not_ready_streak = 0
        for attempt in range(1, args.retries + 1):
            try:
                result = run_with_limit(
                    lambda: invoke_local(args.url, query) if args.url else invoke_deployed(endpoint, args.agent, version, query),
                    args.case_timeout)
            except Exception as exc:  # noqa: BLE001 - reported as a failed case (including CaseTimeout)
                result = {"status": 0, "text": "", "tool_calls": [], "done": False, "errors": [str(exc) or type(exc).__name__]}
            if args.dataset and policy_blocked(result):
                # An adversarial row the platform refuses before the agent answers is a refusal, not a broken agent.
                result = as_refusal(result)
            forbid_tools = bool(args.dataset and category == POLICY_SAFETY)
            problems = check(result, expected, forbid_tools=forbid_tools)
            not_ready_streak = not_ready_streak + 1 if not_ready(result) else 0
            if not_ready_streak >= NOT_READY_LIMIT and problems:
                problems.append(f"agent never became ready in {not_ready_streak} sessions: it is likely crashing at "
                                f"startup; run `azd ai agent monitor {args.agent} --session-id <session>`")
                break
            if not should_retry(result, problems, bool(args.dataset)) or attempt == args.retries:
                break
            print(f"  case {index} attempt {attempt} failed ({retry_detail(result, problems)}); retrying in 30s",
                  flush=True)
            time.sleep(30)
        elapsed = round(time.monotonic() - case_started, 1)
        routing_ok = not result["tool_calls"] if forbid_tools else all(t in result["tool_calls"] for t in expected)
        results.append({"case": index, "category": category, "query": query, "expected_tools": expected,
                        "called_tools": result["tool_calls"], "completed": result["done"] and bool(result["text"])
                        and not result["errors"], "routing_ok": routing_ok,
                        "blocked": bool(result.get("blocked")), "problems": problems, "elapsed_s": elapsed, "answer_preview": result["text"][:240]})
        print(f"{'PASS' if not problems else 'FAIL'} case {index} [{category}] {elapsed}s tools={result['tool_calls']}"
              + (f" :: {problems}" if problems else ""), flush=True)
        if args.ask:
            print(f"answer: {result['text']}", flush=True)

    completed = sum(r["completed"] for r in results)
    routed = sum(r["routing_ok"] for r in results)
    summary = {"target": args.url or f"{args.agent}@{version}", "cases": len(results), "completed": completed,
               "routing_ok": routed, "results": results}
    if args.dataset:
        out = ROOT / ".foundry" / "results" / "baseline.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"baseline: {completed}/{len(results)} completed, {routed}/{len(results)} routed correctly -> {out}")
        sys.exit(0 if completed == len(results) else 1)
    sys.exit(0 if all(not r["problems"] for r in results) else 1)


if __name__ == "__main__":
    main()
