"""Create or update one tracking issue per trusted workflow/failing stage."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from quality_gate import METRICS

DEV_WORKFLOW = ".github/workflows/agentops-deploy-dev.yml"
PROD_WORKFLOW = ".github/workflows/agentops-deploy-prod.yml"
DOCTOR_WORKFLOW = ".github/workflows/agentops-doctor.yml"
WORKFLOWS = {DEV_WORKFLOW, PROD_WORKFLOW, DOCTOR_WORKFLOW}
JOB_ORDER = ("test", "evaluate", "readiness", "promote", "resolve", "production", "doctor")
LABEL = "ci-cd"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise RuntimeError("Unexpected GitHub API redirect; check the repository configuration")


class GitHub:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected repository in owner/name format")
        if not token:
            raise ValueError("GH_TOKEN or GITHUB_TOKEN is required")
        self.repository = repository
        self.prefix = f"/repos/{repository}"
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            f"https://api.github.com{self.prefix}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with self.opener.open(request, timeout=30) as response:
            return json.load(response)

    def object(self, method: str, path: str, body: dict | None = None) -> dict:
        result = self.request(method, path, body)
        if not isinstance(result, dict):
            raise ValueError(f"GitHub returned an unexpected object for {path}")
        return result

    def pages(self, path: str, key: str | None = None):
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            result = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if key:
                if not isinstance(result, dict):
                    raise ValueError("GitHub pagination returned an unexpected object")
                result = result.get(key)
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise ValueError("GitHub pagination returned an unexpected list")
            yield from result
            if len(result) < 100:
                return
        raise RuntimeError("GitHub pagination exceeded the safety limit; issue lookup is incomplete")


def validate_run(run: dict, run_id: int) -> None:
    if run_id <= 0 or run.get("id") != run_id:
        raise ValueError("Invalid source run identity")
    if run.get("path", "").split("@")[0] not in WORKFLOWS:
        raise ValueError("Only the dev/prod release and scheduled Doctor workflows are supported")
    if run.get("head_branch") != "main" or run.get("event") not in {
            "push", "schedule", "workflow_dispatch", "workflow_run"}:
        raise ValueError("Issue reporting is restricted to trusted main-branch runs")
    if type(run.get("run_attempt")) is not int or run["run_attempt"] < 1:
        raise ValueError("Invalid run attempt")
    if str(run_id) != os.environ.get("GITHUB_RUN_ID") and run.get("status") != "completed":
        raise ValueError("Historical replay requires a completed run")


def primary_failure(jobs: list[dict]) -> dict | None:
    failed = [job for job in jobs if isinstance(job.get("name"), str)
              and not job["name"].startswith("report-failure")
              and job.get("conclusion") in {"failure", "timed_out"}]
    return min(failed, key=lambda job: JOB_ORDER.index(job["name"])
               if job["name"] in JOB_ORDER else len(JOB_ORDER)) if failed else None


def markers(repository: str, run: dict, job: dict) -> tuple[str, str]:
    identity = f"{repository}|{run['path'].split('@')[0]}|{job['name']}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return (
        f"<!-- agent-ci-issue:v1:{digest} -->",
        f"<!-- agent-ci-run:{run['id']}:{run['run_attempt']} -->",
    )


def safe_text(value, limit: int = 160) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")[:limit]
    text = re.sub(r"(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)\s*[:=]\s*)\S+",
                  r"\1[redacted]", text)
    for character in ("\\", "`", "|", "<", ">", "[", "]", "*", "_", "@"):
        text = text.replace(character, f"\\{character}")
    return text


def number(value, percentage: bool = False) -> str:
    if type(value) not in {int, float} or not math.isfinite(value):
        return "unavailable"
    return f"{value:.1%}" if percentage else f"{value:.3g}"


def load_artifact(path: Path, notes: list[str]) -> dict | None:
    if not path.is_file():
        notes.append(f"{path.name} is unavailable; inspect the linked workflow logs.")
        return None
    if path.stat().st_size > 10_000_000:
        notes.append(f"{path.name} exceeds the reporting size limit; inspect the artifact directly.")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        notes.append(f"{path.name} could not be decoded; no scores were inferred.")
        return None
    if not isinstance(data, dict):
        notes.append(f"{path.name} has an unexpected shape; no scores were inferred.")
        return None
    return data


def evidence_for_run(directory: Path, run: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    run_id, attempt = run["id"], run["run_attempt"]
    evaluation = directory / f"evaluation-{run_id}-{attempt}"
    evidence = {}
    path = run["path"].split("@")[0]
    if path == DEV_WORKFLOW:
        for name in ("gate", "baseline", "candidate"):
            evidence[name] = load_artifact(evaluation / f"{name}.json", notes)
        pack = directory / f"agentops-{run_id}-{attempt}" / "release/latest/evidence.json"
    elif path == PROD_WORKFLOW:
        pack = directory / f"production-{run_id}-{attempt}" / "release/latest/evidence.json"
    else:
        pack = directory / f"readiness-{run_id}" / "release/latest/evidence.json"
    evidence["readiness"] = load_artifact(pack, notes)
    return evidence, notes


def failure_description(job: dict, evidence: dict) -> str:
    descriptions = {
        "evaluation_authentication": "Scoring authentication/permissions failed; no agent-quality verdict.",
        "evaluation_service": "Scoring service execution failed; no agent-quality verdict.",
        "evaluation_timeout": "Scoring timed out; no complete quality verdict.",
        "incomplete_evaluation": "Scored evidence is incomplete; this is not proof the agent regressed.",
        "evaluation_contract": "Evaluator output did not match the required contract.",
        "agent_execution": "The agent invocation failed before it could be scored.",
        "quality_regression": "Completed, comparable scores regressed against the baseline.",
        "evaluation_error": "Evaluation evidence is invalid or incomparable.",
    }
    for name in ("candidate", "baseline"):
        report = evidence.get(name) or {}
        if report.get("failure_kind") in descriptions:
            return descriptions[report["failure_kind"]]
    gate = evidence.get("gate") or {}
    if gate.get("decision") in descriptions:
        return descriptions[gate["decision"]]
    if any("scoring infrastructure preflight" in step.get("name", "").lower()
           and step.get("conclusion") == "failure" for step in (job.get("steps") or [])):
        return "Scoring preflight failed before agent deployment; inspect authentication or calibration controls."
    return "A pipeline stage failed; inspect the failed steps and evidence below."


def issue_body(repository: str, run: dict, job: dict, jobs: list[dict],
               evidence: dict, notes: list[str]) -> str:
    fingerprint, occurrence = markers(repository, run, job)
    url = f"https://github.com/{repository}/actions/runs/{run['id']}"
    lines = [
        fingerprint, occurrence, "## Automated CI/CD failure",
        f"- **Workflow:** {safe_text(run['name'])}",
        f"- **Primary failing stage:** `{safe_text(job['name'])}`",
        f"- **Failure category:** {failure_description(job, evidence)}",
        f"- **Run:** [{run['id']}, attempt {run['run_attempt']}]({url})",
        f"- **Source revision:** `{safe_text(run.get('head_sha', 'unavailable'), 64)}`",
        "", "### Failed steps",
    ]
    steps = [step for step in (job.get("steps") or []) if step.get("conclusion") in {"failure", "timed_out"}]
    lines.extend(f"- {safe_text(step['name'])}" for step in steps[:10])
    if not steps:
        lines.append("- No step-level failure detail is available; inspect the job logs.")
    others = [item["name"] for item in jobs if item.get("name") in JOB_ORDER
              and item.get("conclusion") == "failure" and item["name"] != job["name"]]
    if others:
        lines.append(f"\nOther failed stages: {', '.join(safe_text(name) for name in others)}. "
                     "One issue is reported for the primary failure, not each downstream failure.")
    gate = evidence.get("gate")
    if isinstance(gate, dict):
        passed = gate.get("passed")
        decision = "passed" if passed is True else "blocked" if passed is False else "unavailable"
        lines.extend(["", "### Quality-gate evidence", f"Gate decision: `{decision}`",
                      "", "| Metric | Gate | Baseline mean | Candidate mean | Baseline pass rate | Candidate pass rate | Result |",
                      "|---|---|---:|---:|---:|---:|---|"])
        metrics = gate.get("metrics", {})
        if isinstance(metrics, dict):
            for name in METRICS:
                values = metrics.get(name)
                if isinstance(values, dict):
                    gate_kind = values.get("gate") if values.get("gate") in {"case", "aggregate"} else "unknown"
                    result = values.get("status") if values.get("status") in {
                        "pass", "regression", "not_comparable"} else "unknown"
                    lines.append(f"| {name} | {gate_kind} | {number(values.get('baseline_mean'))} | "
                                 f"{number(values.get('candidate_mean'))} | "
                                 f"{number(values.get('baseline_pass_rate'), True)} | "
                                 f"{number(values.get('candidate_pass_rate'), True)} | {result} |")
        failures = gate.get("failures", [])
        if isinstance(failures, list):
            if any(isinstance(item, str) and "below absolute quality floor" in item for item in failures):
                lines.append("\nAn absolute quality floor failed. This can block a candidate even when its average improves.")
            if any(isinstance(item, str) and ("aggregate regression" in item or ": regression" in item)
                   for item in failures):
                lines.append("\nA baseline or per-case regression was reported.")
    for label in ("baseline", "candidate"):
        report = evidence.get(label)
        if not isinstance(report, dict):
            continue
        provenance = report.get("provenance") or {}
        version = str(provenance.get("version", ""))
        if re.fullmatch(r"[1-9][0-9]*", version):
            lines.append(f"\n{label.title()} agent version: `{version}`.")
        state = (report.get("run") or {}).get("status")
        if state in {"completed", "failed", "cancelled", "canceled", "in_progress"}:
            lines.append(f"{label.title()} evaluation status: `{state}`.")
    candidate = evidence.get("candidate") or {}
    cases = []
    for item in candidate.get("output_items", []):
        source = item.get("datasource_item") or {}
        source = source.get("item", source)
        case_id = str(source.get("case_id", ""))
        if not re.fullmatch(r"[0-9]+", case_id):
            continue
        for result in item.get("results", []):
            if result.get("passed") is False and result.get("name") in METRICS:
                cases.append(f"- Case `{case_id}`: `{result['name']}`, score `{number(result.get('score'))}`.")
    if cases:
        lines.extend(["", "### Failing candidate cases (zero-based dataset IDs)", *cases[:20]])
        if len(cases) > 20:
            lines.append(f"- {len(cases) - 20} additional checks omitted; see artifacts.")
    readiness = evidence.get("readiness")
    if isinstance(readiness, dict):
        state = readiness.get("status")
        if state in {"ready", "ready_with_warnings", "blocked"}:
            lines.extend(["", f"### AgentOps readiness: `{state}`"])
        for check in readiness.get("checks", []):
            if check.get("status") in {"blocked", "unknown", "warning"}:
                lines.append(f"- {safe_text(check.get('name', 'Unnamed check'))}: `{check['status']}`.")
    if notes:
        lines.extend(["", "### Evidence availability", *[f"- {note}" for note in dict.fromkeys(notes)]])
    lines.extend([
        "", "### Human triage",
        f"Inspect the [workflow logs and downloadable artifacts]({url}). "
        "Raw prompts, responses, judge text and logs are intentionally not copied into this issue.",
        "Recurring failures of this workflow/stage update this issue. A later green run does not close it. "
        "Review agent behavior, evaluation policy or infrastructure as appropriate, and close manually after resolution.",
        "This reporter does not modify agents, weaken thresholds, or perform remediation.",
    ])
    return "\n".join(lines)


def upsert_issue(api: GitHub, run: dict, job: dict, body: str) -> tuple[str, str]:
    fingerprint, occurrence = markers(api.repository, run, job)
    existing = next((issue for issue in api.pages("/issues?state=open")
                     if "pull_request" not in issue and issue.get("state") == "open"
                     and fingerprint in (issue.get("body") or "")), None)
    if existing:
        url = f"https://github.com/{api.repository}/issues/{existing['number']}"
        if occurrence in (existing.get("body") or "") or any(
            occurrence in (comment.get("body") or "")
            for comment in api.pages(f"/issues/{existing['number']}/comments")
        ):
            return "already-reported", url
        api.object("POST", f"/issues/{existing['number']}/comments", {"body": body})
        return "updated", url
    try:
        api.object("GET", f"/labels/{LABEL}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        api.object("POST", "/labels", {
            "name": LABEL, "color": "B60205", "description": "Automated CI/CD failure requiring human triage",
        })
    created = api.object("POST", "/issues", {
        "title": f"[CI/CD] {run['name']}: {job['name']} failed"[:240],
        "body": body, "labels": [LABEL],
    })
    return "created", f"https://github.com/{api.repository}/issues/{created['number']}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, default=os.environ.get("GITHUB_RUN_ID"), required=False)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--artifacts", type=Path, default=Path(".foundry/results/issue-report"))
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.run_id or not args.repository:
        parser.error("--run-id and --repository (or GitHub environment values) are required")
    api = GitHub(args.repository, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
    run = api.object("GET", f"/actions/runs/{args.run_id}")
    validate_run(run, args.run_id)
    if args.validate_only:
        print(f"Validated trusted source run {args.run_id}, attempt {run['run_attempt']}")
        return
    jobs = list(api.pages(f"/actions/runs/{args.run_id}/attempts/{run['run_attempt']}/jobs", "jobs"))
    primary = primary_failure(jobs)
    if run.get("conclusion") == "cancelled" or primary is None:
        if run.get("conclusion") in {"failure", "timed_out"}:
            raise ValueError("Run failed, but no supported failing stage was found")
        print("No actionable failure; no issue created or closed.")
        return
    evidence, notes = evidence_for_run(args.artifacts, run)
    if os.environ.get("ARTIFACT_DOWNLOAD_OUTCOME") == "failure":
        notes.append("Artifact download failed; this report uses job metadata and any available artifacts.")
    body = issue_body(api.repository, run, primary, jobs, evidence, notes)
    if args.dry_run:
        print(body)
        return
    action, url = upsert_issue(api, run, primary, body)
    print(f"Issue {action}: {url}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(f"\n## Automatic failure tracking\nIssue {action}: {url}\n")


if __name__ == "__main__":
    main()
