"""Foundry Insights (preview) for this agent: scan its traces for failure spikes, latency regressions and quality drops.

    python scripts/insights_ops.py enable --model gpt-5.4 --run-now --wait 900   # scheduled Insights on + a first run now
    python scripts/insights_ops.py scan --model gpt-5.4 --wait 900     # one scan now (the portal's "Run now")
    python scripts/insights_ops.py status                              # the agent's monitor and its latest runs

Insights are driven by one agent insight monitor per agent (what the agent's Insights tab and Settings > Insights show).
`enable` creates the monitor when it is missing, sets the judge model (GPT-5 or newer) and the run interval and turns
scheduled runs on. The first scheduled run is not prompt (none had started 12 minutes after enabling in a live test), so
--run-now also starts one run immediately over the last 7 days of traces, unless a run is already active. Calls the Foundry REST API
(preview header AgentInsights=V1Preview) with your Azure CLI sign-in. Reads usecase.yaml and FOUNDRY_PROJECT_ENDPOINT
(from the environment, .env or the selected azd environment). Writes .foundry/results/insights.json.

The project's managed identity needs Monitoring Reader on the connected Application Insights resource (and Privileged
Monitoring Data Reader when trace content is protected), otherwise runs analyze no traces.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / ".foundry" / "results" / "insights.json"
API_VERSION = "v1"
FEATURE_HEADER = {"Foundry-Features": "AgentInsights=V1Preview"}
DONE = {"succeeded", "failed", "cancelled"}


def load_env() -> None:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                if value.strip():
                    os.environ.setdefault(key.strip(), value.strip())
    if (ROOT / ".azure").is_dir():
        result = subprocess.run(["azd", "env", "get-values"], cwd=ROOT, capture_output=True, text=True)
        for line in result.stdout.splitlines() if result.returncode == 0 else []:
            key, _, value = line.partition("=")
            if key and value:
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


def spec() -> dict:
    return yaml.safe_load((ROOT / "usecase.yaml").read_text())


def endpoint() -> str:
    value = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not value.startswith("https://"):
        sys.exit("FOUNDRY_PROJECT_ENDPOINT is not set (see .env.example)")
    return value


class Monitors:
    """The project's agent insight monitors (REST, preview)."""

    def __init__(self, project_endpoint: str, token: str | None = None, client: httpx.Client | None = None):
        if token is None:
            from azure.identity import AzureCliCredential

            token = AzureCliCredential().get_token("https://ai.azure.com/.default").token
        self.base = f"{project_endpoint.rstrip('/')}/agent_insight_monitors"
        self.http = client or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {token}", **FEATURE_HEADER}

    def _call(self, method: str, path: str = "", body: dict | None = None, content_type: str = "application/json") -> dict:
        headers = {**self.headers, **({"Content-Type": content_type} if body is not None else {})}
        response = self.http.request(method, self.base + path, params={"api-version": API_VERSION}, headers=headers,
                                     content=json.dumps(body) if body is not None else None)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} agent_insight_monitors{path}: HTTP {response.status_code} {response.text[:400]}")
        return response.json() if response.content else {}

    def find(self, agent: str) -> dict | None:
        return next((m for m in self._call("GET").get("data", []) if m.get("agent_name") == agent), None)

    def ensure(self, agent: str, model: str) -> tuple[dict, bool]:
        existing = self.find(agent)
        if existing:
            return existing, False
        return self._call("POST", body={"agent_name": agent, "model_deployment_name": model, "enabled": False}), True

    def update(self, monitor_id: str, **changes) -> dict:
        return self._call("PATCH", f"/{monitor_id}", changes, content_type="application/merge-patch+json")

    def runs(self, monitor_id: str) -> list[dict]:
        return sorted(self._call("GET", f"/{monitor_id}/runs").get("data", []), key=lambda r: r.get("created_at") or 0)

    def run(self, monitor_id: str, run_id: str) -> dict:
        return self._call("GET", f"/{monitor_id}/runs/{run_id}")

    def start_run(self, monitor_id: str) -> dict:
        return self._call("POST", f"/{monitor_id}/runs", {})  # empty: the default 7-day (168 hour) lookback


def agent_name(args) -> str:
    name = args.agent or spec().get("agent_name")
    if not name:
        sys.exit("usecase.yaml has no agent_name; pass --agent <deployed agent name>")
    return name


def follow(monitors: Monitors, monitor_id: str, run: dict | None, wait_seconds: int, interval: int = 20,
           ignore: set | None = None) -> dict | None:
    """Wait for a run to finish (or for a new scheduled run, not one in `ignore`, to appear), up to wait_seconds."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline and (run is None or run.get("status") not in DONE):
        time.sleep(interval)
        if run:
            run = monitors.run(monitor_id, run["id"])
        else:
            run = next((r for r in reversed(monitors.runs(monitor_id)) if r["id"] not in (ignore or set())), None)
        if run:
            print(f"run {run['id']}: {run.get('status')}", flush=True)
    return run


def run_summary(run: dict | None) -> dict:
    if not run:
        return {"status": "not started yet"}
    result = run.get("result") or {}
    keys = ("traces_in_window", "traces_analyzed", "insights_created", "insights_updated", "insights_reopened")
    error = (run.get("error") or {}).get("message")
    return {"id": run.get("id"), "status": run.get("status"), **{k: result[k] for k in keys if k in result},
            **({"error": error[:300]} if error else {})}


def write_state(update: dict) -> dict:
    state = json.loads(STATE.read_text()) if STATE.is_file() else {"schema_version": 1}
    state.update(update, updated_at=datetime.now(timezone.utc).isoformat())
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    return state


def describe(run: dict) -> str:
    counts = " ".join(f"{k}={v}" for k, v in run.items() if k not in ("id", "status", "error"))
    return f"run={run.get('id', '-')} status={run['status']}" + (f" {counts}" if counts else "") + \
        (f" error={run['error']!r}" if run.get("error") else "")


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("enable", "scan", "status"):
        command = sub.add_parser(name)
        command.add_argument("--agent", default=None, help="deployed agent name (default: the use case's agent)")
        if name != "status":
            command.add_argument("--model", required=True, help="judge model deployment (GPT-5 or newer)")
            command.add_argument("--wait", type=int, default=0, help="seconds to follow the run")
        if name == "enable":
            command.add_argument("--interval-hours", type=float, default=24, help="hours between scheduled runs")
            command.add_argument("--run-now", action="store_true", help="also start a first run now")
    args = parser.parse_args()
    agent = agent_name(args)
    monitors = Monitors(endpoint())
    if args.command == "status":
        monitor = monitors.find(agent)
        print(json.dumps({"monitor": monitor, "runs": [run_summary(r) for r in monitors.runs(monitor["id"])] if monitor else []},
                         indent=2))
        return
    monitor, created = monitors.ensure(agent, args.model)
    if args.command == "enable":
        before = {r["id"] for r in monitors.runs(monitor["id"])}
        monitor = monitors.update(monitor["id"], enabled=True, run_interval_hours=args.interval_hours,
                                  model_deployment_name=args.model)
        if not monitor.get("enabled"):
            sys.exit(f"monitor {monitor['id']} did not report enabled after the update")
        runs = monitors.runs(monitor["id"])
        latest = next((r for r in reversed(runs) if r["id"] not in before), None)
        active = next((r for r in reversed(runs) if r.get("status") in ("queued", "in_progress")), None)
        if args.run_now and not (latest or active):
            latest = monitors.start_run(monitor["id"])
        latest = latest or active
        run = run_summary(follow(monitors, monitor["id"], latest, args.wait, ignore=before) if args.wait else latest)
        write_state({"monitor": {k: monitor.get(k) for k in ("id", "agent_name", "enabled", "run_interval_hours",
                                                              "model_deployment_name", "next_scheduled_run_at")},
                     "first_run": run})
        print(f"INSIGHTS_ENABLED monitor={monitor['id']} ({'created' if created else 'existing'}) agent={agent} "
              f"every={monitor.get('run_interval_hours'):g}h judge={monitor.get('model_deployment_name')} {describe(run)}")
        if run.get("status") in ("failed", "cancelled"):
            sys.exit(f"the first Insights run ended {run['status']}: {run.get('error', '')}")
        return
    run = run_summary(follow(monitors, monitor["id"], monitors.start_run(monitor["id"]), args.wait))
    write_state({"last_scan": run})
    print(f"INSIGHTS_SCAN monitor={monitor['id']} {describe(run)}")
    if run.get("status") in ("failed", "cancelled"):
        sys.exit(f"Insights run ended {run['status']}: {run.get('error', '')}")


if __name__ == "__main__":
    main()
