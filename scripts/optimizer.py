"""Agent Optimizer helper for this repository: run it once from the Factory or Copilot, or read its results.

    python scripts/optimizer.py run [--max-candidates 2] [--min-improvement 0.02]   submit, wait, decide, promote a clear winner
    python scripts/optimizer.py check                                               is the baseline optimizer-ready?
    python scripts/optimizer.py summary                                             last baseline / judged / optimizer results
    python scripts/optimizer.py decide --status-file status.txt --output decision.json   (CI building block)
    python scripts/optimizer.py promote --candidate cand_opt_..._0003                     (CI building block)

`run` submits `azd ai agent optimize` against the DEV project. Candidates are draft agent versions that never serve traffic. A
candidate is promoted only when it beats the baseline by `--min-improvement`: its configuration then replaces
`.agent_configs/baseline/` in this working tree. Nothing is deployed or committed here; deploying is a separate, reviewed step.

The optimizer needs a newer azd `azure.ai.agents` extension than agent deployment does, so `run` works in its own copy of the
azd configuration (F100_OPTIMIZER_AZD_DIR, default ~/.foundry100/azd-optimizer) and never changes your azd extensions.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / ".agent_configs"
RESULTS = ROOT / ".foundry" / "results" / "optimizer"
EXTENSION = "azure.ai.agents"
EXTENSION_VERSION = os.environ.get("AZD_AI_AGENTS_EXTENSION_VERSION", "1.0.0-beta.17")
TERMINAL = {"succeeded", "failed", "cancelled", "canceled", "completed"}
CANCEL_WAIT_MINUTES = 10  # after a time-boxed cancel, wait this long for the job to stop before deciding
PORTAL = re.compile(r"https://ai\.azure\.com/[^\s]*/optimization/opt_[0-9a-f]+")

ESCAPES = re.compile(r"\x1b\]8;;.*?(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")
JOB = re.compile(r"\bJob ID:\s*(opt_[0-9a-f]+)")
STATUS = re.compile(r"^\s*Status:\s*(\w+)", re.MULTILINE)
WARNING = re.compile(r"^\s*Warning:\s*(.+)$", re.MULTILINE)
RESULT_ROW = re.compile(r"^\s*(?P<name>\S(?:.*?\S)?)\s+(?P<star>★\s+)?(?P<score>-?\d+\.\d+)(?:\s|$)")
CANDIDATE_ROW = re.compile(r"^\s*(?P<star>★\s+)?(?P<name>\S(?:.*?\S)?)\s+(?P<id>cand_[A-Za-z0-9_]+)\s*$")
CANDIDATE_ID = re.compile(r"cand_[A-Za-z0-9_]+")


def _section(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == header:
            block = []
            for following in lines[index + 1:]:
                if not following.strip():
                    if block:
                        break
                    continue
                block.append(following)
            return block
    return []


def parse_status(text: str) -> dict:
    text = ESCAPES.sub("", text)
    job, status = JOB.search(text), STATUS.search(text)
    results = []
    for line in _section(text, "Results:"):
        match = RESULT_ROW.match(line)
        if match:
            results.append({"name": match["name"], "score": float(match["score"]), "best": bool(match["star"])})
    candidates = []
    for line in _section(text, "Candidate IDs:"):
        match = CANDIDATE_ROW.match(line)
        if match:
            candidates.append({"name": match["name"], "id": match["id"], "best": bool(match["star"])})
    return {
        "job_id": job[1] if job else None,
        "status": status[1].lower() if status else None,
        "warnings": WARNING.findall(text),
        "results": results,
        "candidates": candidates,
    }


def decide(parsed: dict, min_improvement: float) -> dict:
    """Return {'action': 'promote'|'none'|'fail', ...}; only a clear, scored improvement is promoted."""
    decision = {**parsed, "action": "none", "min_improvement": min_improvement}
    if parsed["status"] != "succeeded":
        return {**decision, "action": "fail", "reason": f"optimizer job status is {parsed['status'] or 'unknown'}"}
    baseline = next((r for r in parsed["results"] if r["name"] == "baseline"), None)
    best = next((r for r in parsed["results"] if r["best"]), None)
    winner = next((c for c in parsed["candidates"] if c["best"]), None)
    if baseline is None or best is None or winner is None:
        return {**decision, "action": "fail", "reason": "could not read baseline/best scores from optimizer output"}
    decision.update(baseline_score=baseline["score"], winner_score=best["score"],
                    candidate=winner["id"], candidate_name=winner["name"])
    if winner["name"] == "baseline" or best["name"] == "baseline":
        return {**decision, "reason": "the baseline is still the best configuration"}
    improvement = round(best["score"] - baseline["score"], 6)
    decision["improvement"] = improvement
    if improvement < min_improvement:
        return {**decision, "reason": f"improvement {improvement:+.3f} is below the {min_improvement:.3f} threshold"}
    return {**decision, "action": "promote", "reason": f"candidate improves the baseline by {improvement:+.3f}"}


def summary_markdown(decision: dict, portal_url: str = "") -> str:
    job = f"`{decision.get('job_id') or 'unknown'}`" + (f" ([portal]({portal_url}))" if portal_url else "")
    lines = ["## Agent Optimizer", "", f"- Job: {job}", f"- Status: `{decision.get('status')}`",
             f"- Decision: **{decision['action']}**: {decision['reason']}"]
    if "baseline_score" in decision:
        lines.append(f"- Baseline score: {decision['baseline_score']:.3f}; best score: {decision['winner_score']:.3f}")
    if decision["action"] == "promote":
        lines.append(f"- Winning candidate: `{decision['candidate']}` ({decision['candidate_name']})")
    if decision.get("results"):
        lines += ["", "| Candidate | Score | Best |", "|---|---:|:---:|"]
        lines += [f"| {r['name']} | {r['score']:.3f} | {'★' if r['best'] else ''} |" for r in decision["results"]]
    lines += [f"\n> {warning}" for warning in decision.get("warnings", [])]
    return "\n".join(lines) + "\n"


def promote(candidate: str, configs: Path | None = None) -> list[str]:
    """Replace baseline/ with the applied candidate directory and remove the candidate directory."""
    if not CANDIDATE_ID.fullmatch(candidate):
        raise ValueError(f"invalid candidate id: {candidate!r}")
    configs = configs or CONFIGS
    source, baseline = configs / candidate, configs / "baseline"
    if not (source / "metadata.yaml").is_file():
        raise FileNotFoundError(f"{source}/metadata.yaml not found; run 'azd ai agent optimize apply' first")
    shutil.rmtree(baseline)
    shutil.copytree(source, baseline)
    shutil.rmtree(source)
    return sorted(p.relative_to(baseline).as_posix() for p in baseline.rglob("*") if p.is_file())


def check(root: Path = ROOT) -> dict:
    """Optimizer readiness: a loadable baseline with model and instructions, and a dataset of `query` rows."""
    import yaml
    from azure.ai.agentserver.optimization import load_config, load_skills_from_dir

    config = load_config(config_dir=root / ".agent_configs")
    if config is None:
        raise ValueError("load_config returned None: .agent_configs/baseline is missing")
    if not config.skills and config.skills_dir:
        config.skills.extend(load_skills_from_dir(Path(config.skills_dir)))
    if not config.model or not config.compose_instructions():
        raise ValueError("optimizer baseline has no model or no instructions")
    spec = yaml.safe_load((root / "eval.yaml").read_text())
    rows = [json.loads(line) for line in (root / spec["dataset"]["local_uri"]).read_text().splitlines() if line.strip()]
    if not rows or not all(isinstance(row.get("query"), str) and row["query"].strip() for row in rows):
        raise ValueError("every optimizer dataset row needs a non-empty top-level 'query'")
    return {"source": str(config.source), "model": config.model, "tools": len(config.tool_definitions or []),
            "skills": len(config.skills), "dataset_rows": len(rows)}


def _azd_dir() -> Path:
    """A private copy of the azd configuration that carries the newer agents extension (and the current sign-in)."""
    base = Path(os.environ.get("AZD_CONFIG_DIR") or Path.home() / ".azd")
    target = Path(os.environ.get("F100_OPTIMIZER_AZD_DIR") or Path.home() / ".foundry100" / "azd-optimizer")
    if base.resolve() == target.resolve():
        return target
    if not target.exists():
        shutil.copytree(base, target, symlinks=True)
    else:
        for name in ("auth", "config.json"):  # keep the sign-in current; extensions stay as installed here
            source = base / name
            if source.is_dir():
                shutil.copytree(source, target / name, symlinks=True, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, target / name)
    return target


def _azd(*args: str, env: dict, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["azd", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)


def _extension_ready(env: dict) -> None:
    listed = _azd("extension", "list", "--output", "json", env=env, timeout=120)
    try:
        installed = next((e.get("installedVersion") or e.get("version") for e in json.loads(listed.stdout) if e.get("id") == EXTENSION), None)
    except (json.JSONDecodeError, TypeError):
        installed = None
    if installed == EXTENSION_VERSION:
        return
    result = _azd("extension", "install", EXTENSION, "--version", EXTENSION_VERSION, "--force", "--no-prompt", env=env, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"could not install azd extension {EXTENSION} {EXTENSION_VERSION}: {(result.stdout + result.stderr)[-400:]}")


def reflection_model() -> str | None:
    """The deployment that proposes candidates: OPTIMIZE_MODEL, else .foundry/optimizer.json written at generation time.

    The service only accepts some models for this role (gpt-5, gpt-5.1, gpt-5.2, gpt-5.4, gpt-5.5, deepseek-v4-pro,
    deepseek-v3.2), which can differ from the agent's own model."""
    if os.environ.get("OPTIMIZE_MODEL", "").strip():
        return os.environ["OPTIMIZE_MODEL"].strip()
    chosen = ROOT / ".foundry" / "optimizer.json"
    if chosen.is_file():
        return (json.loads(chosen.read_text()).get("optimize_model") or "").strip() or None
    return None


def _agent_name() -> str:
    import yaml

    return yaml.safe_load((ROOT / "eval.yaml").read_text())["agent"]["name"]


def run(max_candidates: int, min_improvement: float, optimize_model: str | None, timeout_minutes: int, poll_seconds: int = 30) -> dict:
    """Submit one optimizer job, wait for it, decide and (for a clear winner) apply and promote it. Returns the decision."""
    import yaml

    print(f"optimizer-ready: {json.dumps(check())}", flush=True)
    agent = _agent_name()
    env = {**os.environ, "AZD_CONFIG_DIR": str(_azd_dir())}
    _extension_ready(env)
    model = optimize_model or reflection_model() or yaml.safe_load((ROOT / "eval.yaml").read_text())["agent"].get("model")
    RESULTS.mkdir(parents=True, exist_ok=True)
    metadata = CONFIGS / "baseline" / "metadata.yaml"
    before = metadata.read_bytes()  # submitting re-serializes the baseline metadata; keep the tree identical
    submitted = _azd("ai", "agent", "optimize", "--agent", agent, "--config", "eval.yaml", "--optimize-model", model,
                     "--max-candidates", str(max_candidates), "--no-wait", "--no-prompt", env=env, timeout=900)
    metadata.write_bytes(before)
    text = ESCAPES.sub("", submitted.stdout + submitted.stderr)
    (RESULTS / "submit.txt").write_text(text)
    job = JOB.search(text) or re.search(r"opt_[0-9a-f]+", text)
    if submitted.returncode != 0 or not job:
        hint = (" Use a deployment of one of the models the optimizer accepts for proposing candidates: pass --optimize-model "
                "or set OPTIMIZE_MODEL." if "reflection model" in text else "")
        raise RuntimeError(f"optimizer submission failed: {text[-600:]}{hint}")
    job_id = job.group(1) if job.groups() else job.group(0)
    portal = PORTAL.search(text)
    print(f"optimizer job {job_id} submitted (candidates are draft versions; live traffic is untouched)", flush=True)
    deadline, status_text, time_boxed = time.time() + timeout_minutes * 60, "", False
    while time.time() < deadline:
        status = _azd("ai", "agent", "optimize", "status", job_id, "--no-prompt", env=env, timeout=300)
        status_text = ESCAPES.sub("", status.stdout + status.stderr)
        state = (parse_status(status_text)["status"] or "").lower()
        print(f"  optimizer status: {state or 'unknown'}", flush=True)
        if state in TERMINAL:
            break
        time.sleep(poll_seconds)
    else:
        # Time box: stop the job and decide from the candidates that were already scored (a partial run is still real data).
        # Cancel is asynchronous: in-flight candidate evaluations keep invoking the agent and the shared model until the
        # job is terminal, which slows (and times out) whatever runs next. Wait for that before returning.
        _azd("ai", "agent", "optimize", "cancel", job_id, "--no-prompt", env=env, timeout=120)
        cancel_deadline = time.time() + CANCEL_WAIT_MINUTES * 60
        while True:
            final = _azd("ai", "agent", "optimize", "status", job_id, "--no-prompt", env=env, timeout=300)
            status_text, time_boxed = ESCAPES.sub("", final.stdout + final.stderr), True
            state = (parse_status(status_text)["status"] or "").lower()
            if state in TERMINAL:
                print(f"  optimizer job {job_id} stopped ({state})", flush=True)
                break
            if time.time() >= cancel_deadline:
                print(f"  warning: optimizer job {job_id} still {state or 'unknown'} {CANCEL_WAIT_MINUTES} minutes after "
                      "cancel; its remaining evaluations may slow the next agent calls", flush=True)
                break
            print(f"  optimizer cancel requested; waiting for the job to stop ({state or 'unknown'})", flush=True)
            time.sleep(poll_seconds)
    (RESULTS / "status.txt").write_text(status_text)
    parsed = parse_status(status_text)
    if time_boxed:
        if not any(r["name"] != "baseline" for r in parsed["results"]):
            raise RuntimeError(f"optimizer job {job_id} scored no candidate within {timeout_minutes} minutes and was cancelled")
        if not any(r["best"] for r in parsed["results"]):  # a cancelled job marks no best row: take the top score
            top = max(parsed["results"], key=lambda r: r["score"])
            top["best"] = True
            for candidate in parsed["candidates"]:
                candidate["best"] = candidate["name"] == top["name"]
        parsed["status"] = "succeeded"  # decided on the scored candidates only
    decision = decide(parsed, min_improvement)
    if time_boxed:
        decision["time_boxed"] = f"stopped after {timeout_minutes} minutes; decided on the candidates scored so far"
        decision["reason"] += f" (time-boxed: stopped after {timeout_minutes} minutes)"
    decision["portal_url"] = portal.group(0) if portal else ""
    (RESULTS / "decision.json").write_text(json.dumps(decision, indent=2) + "\n")
    (RESULTS / "summary.md").write_text(summary_markdown(decision, decision["portal_url"]))
    if decision["action"] == "promote":
        applied = _azd("ai", "agent", "optimize", "apply", "--agent", agent, "--candidate", decision["candidate"], "--no-prompt", env=env, timeout=600)
        if applied.returncode != 0:
            raise RuntimeError(f"could not apply {decision['candidate']}: {(applied.stdout + applied.stderr)[-500:]}")
        promote(decision["candidate"])
        print(f"promoted {decision['candidate']} to .agent_configs/baseline", flush=True)
    print(f"{decision['action']}: {decision['reason']}", flush=True)
    return decision


def results_summary() -> str:
    """Readable results of the baseline, judged evaluation and optimizer runs recorded under .foundry/results/."""
    lines = []
    baseline = ROOT / ".foundry" / "results" / "baseline.json"
    if baseline.is_file():
        data = json.loads(baseline.read_text())
        lines.append(f"Baseline: {data.get('completed')}/{data.get('cases')} completed, {data.get('routing_ok')}/{data.get('cases')} routed correctly")
    judged = ROOT / ".foundry" / "results" / "judged" / "summary.json"
    if judged.is_file():
        lines.append("Judged evaluation: " + json.dumps(json.loads(judged.read_text()))[:600])
    decision = RESULTS / "decision.json"
    if decision.is_file():
        data = json.loads(decision.read_text())
        lines.append(f"Optimizer job {data.get('job_id')}: {data['action']} - {data['reason']}")
        if "baseline_score" in data:
            lines.append(f"  baseline {data['baseline_score']:.3f}, best {data['winner_score']:.3f}")
    return "\n".join(lines) or "No results recorded yet: run the baseline (scripts/smoke.py --dataset ...) or /faf-optimize-agent."



def _write_outputs(values: dict) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in values.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    decide_cmd = commands.add_parser("decide")
    decide_cmd.add_argument("--status-file", required=True, type=Path)
    decide_cmd.add_argument("--min-improvement", type=float, default=0.02)
    decide_cmd.add_argument("--portal-url", default="")
    decide_cmd.add_argument("--output", required=True, type=Path)
    decide_cmd.add_argument("--summary", type=Path)
    promote_cmd = commands.add_parser("promote")
    promote_cmd.add_argument("--candidate", required=True)
    commands.add_parser("check")
    commands.add_parser("summary")
    run_cmd = commands.add_parser("run")
    run_cmd.add_argument("--max-candidates", type=int, default=2)
    run_cmd.add_argument("--min-improvement", type=float, default=0.02)
    run_cmd.add_argument("--optimize-model", default=None, help="model that proposes candidates (default: the agent's model)")
    run_cmd.add_argument("--timeout-minutes", type=int, default=40, help="time box: stop the job and decide on the scored candidates")
    args = parser.parse_args(argv)

    if args.command == "check":
        print(f"optimizer-ready: {json.dumps(check())}")
        return 0
    if args.command == "summary":
        print(results_summary())
        return 0
    if args.command == "run":
        try:
            decision = run(args.max_candidates, args.min_improvement, args.optimize_model, args.timeout_minutes)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            print(f"optimizer failed: {exc}", file=sys.stderr)
            return 1
        return 1 if decision["action"] == "fail" else 0
    if args.command == "promote":
        print(f"promoted {args.candidate} to .agent_configs/baseline: {promote(args.candidate)}")
        return 0

    decision = decide(parse_status(args.status_file.read_text()), args.min_improvement)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(decision, indent=2) + "\n")
    summary = summary_markdown(decision, args.portal_url)
    if args.summary:
        args.summary.write_text(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(summary)
    _write_outputs({"action": decision["action"], "candidate": decision.get("candidate", ""),
                    "job_id": decision.get("job_id") or ""})
    print(f"{decision['action']}: {decision['reason']}")
    return 1 if decision["action"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
