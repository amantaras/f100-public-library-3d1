"""Pin production and promote the exact evaluated immutable version, without rebuilding."""

import argparse
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
import httpx
import yaml

from quality_gate import compare
from provenance import artifact_identity, credential as get_credential, source_revision
from evaluate import invoke


def selection(version: str) -> dict:
    if not re.fullmatch(r"[1-9][0-9]*", version):
        raise ValueError("An explicit numeric version is required; never use @latest")
    return {"version_selection_rules": [
        {"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100}
    ]}


def route(project, agent: str) -> str:
    endpoint = project.agents.get(agent).as_dict()["agent_endpoint"]
    rules = endpoint["version_selector"]["version_selection_rules"]
    if len(rules) != 1 or rules[0]["type"] != "FixedRatio" or rules[0]["traffic_percentage"] != 100:
        raise ValueError("Production must route 100% to one explicitly pinned version")
    version = rules[0]["agent_version"]
    selection(version)
    return version


def pin(project, agent: str, version: str) -> None:
    deployed = project.agents.get_version(agent, version).as_dict()
    if deployed.get("status") != "active":
        raise ValueError(f"{agent}:{version} is not active")
    project.beta.agents.patch_agent_details(agent, body={
        "agent_endpoint": {"protocols": ["invocations"], "version_selector": selection(version)}
    })
    if route(project, agent) != version:
        raise RuntimeError("Endpoint update did not persist")


def prepare(project, agent: str, manifest: Path, candidate_pr: str | None = None) -> str:
    baseline = route(project, agent)
    config = yaml.safe_load(manifest.read_text())
    if config["name"] != agent:
        raise ValueError("Manifest and target agent differ")
    config["agent_endpoint"] = {
        "protocols": ["invocations"], "version_selector": selection(baseline)
    }
    metadata = config.setdefault("metadata", {})
    metadata["git_sha"] = source_revision()
    if candidate_pr is not None:
        # AgentOps convention: mark throwaway PR candidates so they are never mistaken
        # for a deployed version of record.
        if not re.fullmatch(r"[1-9][0-9]*", candidate_pr):
            raise ValueError("Pull request number must be numeric")
        metadata.update(agentops_candidate="true", agentops_pr=candidate_pr,
                        agentops_created_at=datetime.now(timezone.utc).isoformat())
    manifest.write_text(yaml.safe_dump(config, sort_keys=False))
    return baseline


def inspect_candidate(project, agent: str, baseline: str, candidate: str) -> dict:
    selection(baseline)
    selection(candidate)
    if route(project, agent) != baseline:
        raise ValueError("Production changed during candidate deployment")
    identity = artifact_identity(project, agent, candidate)
    if identity["deployed_git_sha"] != source_revision():
        raise ValueError("Candidate does not represent the checked-out source revision")
    equivalent = False
    if candidate != baseline:
        # A new commit that does not touch the packaged agent (for example CI-only changes)
        # still gets a new version number. Same source content hash and definition means the
        # agent did not change, so a "regression" could only be sampling or judge noise.
        serving = artifact_identity(project, agent, baseline)
        equivalent = (serving["artifact"] == identity["artifact"]
                      and serving["definition_sha256"] == identity["definition_sha256"])
    return {
        "status": "unchanged" if candidate == baseline or equivalent else "candidate",
        "agent": agent, "baseline_version": baseline, "candidate_version": candidate,
        "equivalent_artifact": equivalent,
        "identity": identity, "evaluation_performed": False, "promotion_performed": False,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def promote(project, endpoint: str, agent: str, baseline: dict, candidate: dict) -> None:
    result = compare(baseline, candidate)
    if not result["passed"]:
        raise ValueError(f"Promotion blocked: {result['failures']}")
    old, new = baseline["provenance"], candidate["provenance"]
    if new["agent"] != agent or new["endpoint"] != endpoint.rstrip("/"):
        raise ValueError("Evaluation belongs to a different deployment")
    if old["version"] == new["version"]:
        raise ValueError("Candidate must be a new immutable version")
    if route(project, agent) != old["version"]:
        raise ValueError("Production changed while evaluating; rerun against the new baseline")
    identity = artifact_identity(project, agent, new["version"])
    if any(identity[key] != new.get(key) for key in identity):
        raise ValueError("Candidate artifact changed or release provenance is missing")
    if new["evaluation_git_sha"] != source_revision() or new["deployed_git_sha"] != source_revision():
        raise ValueError("Candidate was not built and evaluated from this source revision")
    pin(project, agent, new["version"])
    print(f"Promoted {agent}:{new['version']} (previous baseline {old['version']})")


def smoke_or_rollback(project, credential, endpoint: str, agent: str, previous: str, candidate: str):
    try:
        if route(project, agent) != candidate:
            raise RuntimeError("Production route does not match the promoted candidate")
        # Smoke the default client stream (no opt-in tool events).
        answer = invoke(project, credential, endpoint, agent, candidate,
                        "I'd like to reserve \"The Quantum Library\" by ISBN 978-0-525-55555-5. My patron ID is P20261012.",
                        include_tool_events=False)["text"]
    except (AzureError, httpx.HTTPError, RuntimeError, ValueError):
        if route(project, agent) == candidate:
            pin(project, agent, previous)
            print(f"Smoke test failed; rolled back {agent} to {previous}")
        raise
    print(f"Post-promotion smoke passed ({len(answer)} characters)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pin", "prepare", "assert", "inspect-candidate", "promote"])
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--agent", default="public-library-3d1")
    parser.add_argument("--version")
    parser.add_argument("--baseline-version")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--manifest", type=Path, default=Path("agent.yaml"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--candidate-pr", help="Mark the deployed version as a PR candidate")
    args = parser.parse_args()
    with get_credential() as credential, AIProjectClient(
        endpoint=args.endpoint, credential=credential, allow_preview=True
    ) as project:
        if args.command == "pin":
            if not args.version:
                parser.error("pin requires --version")
            pin(project, args.agent, args.version)
        elif args.command == "prepare":
            version = prepare(project, args.agent, args.manifest, args.candidate_pr)
            print(f"Production remains pinned to {version}")
            if os.getenv("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"baseline_version={version}\n")
        elif args.command == "assert":
            if not args.version or route(project, args.agent) != args.version:
                raise ValueError("Production routing changed unexpectedly")
        elif args.command == "inspect-candidate":
            if not args.version or not args.baseline_version or not args.record:
                parser.error("inspect-candidate requires --version, --baseline-version and --record")
            record = inspect_candidate(project, args.agent, args.baseline_version, args.version)
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(json.dumps(record, indent=2) + "\n")
            changed = record["status"] == "candidate"
            print(f"Candidate {args.version}: {record['status']}; production remains {args.baseline_version}")
            if not changed:
                print("No agent-version change: no new evaluation or promotion will be claimed.")
            if os.getenv("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"changed={str(changed).lower()}\ncandidate_version={args.version}\n")
        else:
            if not args.baseline or not args.candidate:
                parser.error("promote requires --baseline and --candidate")
            baseline = json.loads(args.baseline.read_text())
            candidate = json.loads(args.candidate.read_text())
            promote(project, args.endpoint, args.agent, baseline, candidate)
            smoke_or_rollback(project, credential, args.endpoint, args.agent,
                              baseline["provenance"]["version"], candidate["provenance"]["version"])
            record = {
                "status": "promoted",
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "agent": args.agent,
                "previous_version": baseline["provenance"]["version"],
                "selected_version": route(project, args.agent),
                "candidate_provenance": candidate["provenance"],
                "eval_id": candidate["run"]["eval_id"],
                "eval_run_id": candidate["run"]["id"],
                "smoke_passed": True,
            }
            args.candidate.with_name("deployment.json").write_text(json.dumps(record, indent=2) + "\n")
