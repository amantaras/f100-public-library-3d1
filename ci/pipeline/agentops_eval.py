"""Run ``agentops eval run`` unchanged, scoring dataset rows concurrently.

It also adapts Responses ``function_call`` items to the ``tool_call`` shape the Foundry
tool evaluators expect (see ``tool_calls_for_evaluators``).

AgentOps 0.15 scores rows one at a time (about 8 minutes for 15 cases with
nine judges). This wrapper keeps AgentOps' own CLI, evaluator selection,
thresholds, result files and exit codes, and only runs the per-row
``_evaluate_row`` calls in a thread pool. Rows are returned in dataset order.

Cloud runs (the agent is invoked by Foundry) get a longer wait: AgentOps polls a
Foundry evaluation run for 10 minutes (300 polls of 2s), while a ~28-case dataset of
a hosted agent commonly needs 12-15 minutes. The wait is ``CLOUD_TIMEOUT_MINUTES``
(override with ``AGENTOPS_CLOUD_TIMEOUT_MINUTES``), polled every 10 seconds; it stays
under the 45-minute release jobs.

Usage: python ci/agentops_eval.py [--concurrency N] eval run --config ... --output ...
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable


CLOUD_TIMEOUT_MINUTES = 30.0
CLOUD_POLL_SECONDS = 10.0


def cloud_wait_minutes(environ: dict[str, str] | None = None) -> float:
    raw = (environ if environ is not None else os.environ).get("AGENTOPS_CLOUD_TIMEOUT_MINUTES", "").strip()
    try:
        value = float(raw) if raw else CLOUD_TIMEOUT_MINUTES
    except ValueError:
        raise SystemExit(f"AGENTOPS_CLOUD_TIMEOUT_MINUTES must be a number of minutes, got {raw!r}") from None
    if value <= 0:
        raise SystemExit("AGENTOPS_CLOUD_TIMEOUT_MINUTES must be greater than zero")
    return value


def patient_cloud_runs(run: Callable[..., Any], minutes: float, interval: float = CLOUD_POLL_SECONDS) -> Callable[..., Any]:
    """A drop-in ``run_on_foundry_cloud`` that waits up to ``minutes`` for the Foundry run (explicit arguments win)."""

    def run_patiently(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("poll_interval_seconds", interval)
        kwargs.setdefault("max_poll_attempts", max(1, math.ceil(minutes * 60 / kwargs["poll_interval_seconds"])))
        return run(*args, **kwargs)

    return run_patiently


def parallel_rows(evaluate_row: Callable[..., Any], rows: list[dict[str, Any]], workers: int) -> Callable[..., Any]:
    """Return a drop-in ``_evaluate_row`` that scores every row on the first call."""
    lock = threading.Lock()
    cache: dict[int, Any] = {}

    def patched(*, row: dict[str, Any], index: int, total: int, progress: Callable[[str], None], **kwargs: Any) -> Any:
        with lock:
            if not cache:
                if total != len(rows) or row != rows[index]:
                    raise RuntimeError("dataset rows do not match the rows AgentOps is evaluating")
                safe = threading.Lock()

                def log(message: str) -> None:
                    with safe:
                        progress(message)

                def one(i: int) -> Any:
                    return evaluate_row(row=rows[i], index=i, total=total, progress=log, **kwargs)

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    cache.update(zip(range(total), pool.map(one, range(total))))
        return cache[index]

    return patched


def tool_calls_for_evaluators(calls: list[Any] | None) -> list[Any] | None:
    """Responses-protocol ``function_call`` items -> the ``tool_call`` shape Foundry tool evaluators read.

    Hosted Responses agents report tool use as ``{"type": "function_call", "call_id",
    "name", "arguments": "<json>"}``; ``ToolCallAccuracyEvaluator`` looks for
    ``{"type": "tool_call", "tool_call_id", "name", "arguments": {...}}`` and otherwise
    reports "No tool calls found".
    """
    if not calls:
        return calls
    converted = []
    for call in calls:
        if isinstance(call, dict) and call.get("type") == "function_call":
            arguments = call.get("arguments")
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                pass
            converted.append({"type": "tool_call", "tool_call_id": call.get("call_id") or call.get("id"),
                              "name": call.get("name"), "arguments": arguments or {}})
        else:
            converted.append(call)
    return converted


def normalized_invoke(invoke: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = invoke(*args, **kwargs)
        result.tool_calls = tool_calls_for_evaluators(result.tool_calls)
        return result
    return wrapped


def actual_tool_calls_for_accuracy(run_evaluator: Callable[..., Any]) -> Callable[..., Any]:
    """Score ToolCallAccuracyEvaluator on the calls the agent made in this run.

    AgentOps maps ``$tool_calls`` from the dataset row, so a live run hands the
    evaluator no calls ("No tool calls found"). The conversational evaluators already
    receive the observed calls; this gives the accuracy judge the same evidence.
    """
    def wrapped(runtime: Any, *, row: dict[str, Any], actual_tool_calls: Any = None, **kwargs: Any) -> Any:
        if runtime.preset.class_name == "ToolCallAccuracyEvaluator" and actual_tool_calls:
            row = {**row, "tool_calls": actual_tool_calls}
        return run_evaluator(runtime, row=row, actual_tool_calls=actual_tool_calls, **kwargs)
    return wrapped


def cloud_tool_mapping(build: Callable[[Any], dict[str, str]]) -> Callable[[Any], dict[str, str]]:
    def wrapped(preset: Any) -> dict[str, str]:
        if preset.class_name == "ToolCallAccuracyEvaluator":
            return {"query": "{{item.input}}", "response": "{{sample.output_items}}",
                    "tool_definitions": "{{item.tool_definitions}}"}
        return build(preset)
    return wrapped


def all_output_items(coerce: Callable[[Any], Any]) -> Callable[..., list[dict[str, Any]]]:
    """Download every page of a Foundry eval run's output items.

    AgentOps 0.15.1 reads ``page.data`` (the first page, 20 items); iterating the
    paginator fetches all pages, so a 26-case run reports 26 rows, not 20.
    """
    def download(openai_client: Any, *, eval_id: str, run_id: str, progress: Callable[[str], None]) -> list[dict[str, Any]]:
        pages = openai_client.evals.runs.output_items.list(eval_id=eval_id, run_id=run_id, limit=100)
        items = [item for item in (coerce(raw) for raw in pages) if item is not None]
        progress(f"cloud: downloaded {len(items)} output item(s) (all pages)")
        return items
    return download


def _option(argv: list[str], *names: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg in names and i + 1 < len(argv):
            return argv[i + 1]
        for name in names:
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def main(argv: list[str]) -> int:
    workers = 6
    if argv[:1] == ["--concurrency"]:
        workers, argv = int(argv[1]), argv[2:]
    if argv[:2] != ["eval", "run"]:
        raise SystemExit("usage: agentops_eval.py [--concurrency N] eval run [agentops options]")

    from agentops.cli.app import app
    from agentops.core.config_loader import load_agentops_config
    from agentops.pipeline import orchestrator

    config_path = Path(_option(argv, "--config", "-c") or "agentops.yaml")
    dataset = Path(str(load_agentops_config(config_path).dataset))
    if not dataset.is_absolute():
        dataset = (config_path.parent / dataset).resolve()
    rows = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    orchestrator._evaluate_row = parallel_rows(orchestrator._evaluate_row, rows, workers)
    orchestrator.invocations.invoke = normalized_invoke(orchestrator.invocations.invoke)
    orchestrator.runtime.run_evaluator = actual_tool_calls_for_accuracy(orchestrator.runtime.run_evaluator)
    # Cloud execution: judge tool use from the agent's own output items (Foundry's
    # agent-evaluation mapping) instead of dataset columns.
    from agentops.pipeline import cloud_runner
    cloud_runner._build_cloud_data_mapping = cloud_tool_mapping(cloud_runner._build_cloud_data_mapping)
    cloud_runner._list_output_items = all_output_items(cloud_runner._coerce_output_item_to_dict)
    cloud_runner.run_on_foundry_cloud = patient_cloud_runs(cloud_runner.run_on_foundry_cloud, cloud_wait_minutes())
    try:
        app(args=argv, prog_name="agentops")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
