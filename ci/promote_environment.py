"""Promote the exact artifact that passed the dev gate into another Foundry project (production).

Agent versions are project-scoped, so production cannot reuse the dev version number.
Identity is the artifact, not the commit: production proves its new version has the same
source content hash (packaged code, instructions and skills) and toolbox content that dev
evaluated. A later commit that only changes CI files can therefore release the promoted
artifact, while any agent change must first pass the dev gate.
Quality is gated in dev; production verifies identity, runs smoke checks, pins routing
and rolls back to the previous production version when a smoke check fails.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError, ResourceNotFoundError
import httpx
import yaml

from evaluate import invoke
from provenance import (artifact_identity, credential as get_credential, source_revision,
                        toolbox_fingerprint, toolbox_reference)
from quality_gate import compare, load_rows, validate
from release import pin, prepare, route, selection

NO_VERSION = "none"
ROUTING_SMOKE_CASE = 0  # safety-critical case: must call f100_public_library_3d1_fine_fees


def current_route(project, agent: str) -> str:
    """Pinned production version, or NO_VERSION if production was never released."""
    try:
        endpoint = project.agents.get(agent).as_dict().get("agent_endpoint") or {}
    except ResourceNotFoundError:
        return NO_VERSION
    rules = (endpoint.get("version_selector") or {}).get("version_selection_rules") or []
    if len(rules) == 1 and rules[0].get("agent_version") == "@latest":
        # azd's default routing after a first deploy; no verified release was ever pinned.
        return NO_VERSION
    return route(project, agent)


def prepare_environment(project, agent: str, manifest: Path) -> str:
    """Pin the existing production route during deployment, or bootstrap the first version."""
    if current_route(project, agent) != NO_VERSION:
        return prepare(project, agent, manifest)
    config = yaml.safe_load(manifest.read_text())
    if config["name"] != agent:
        raise ValueError("Manifest and target agent differ")
    config.pop("agent_endpoint", None)
    config.setdefault("metadata", {})["git_sha"] = source_revision()
    manifest.write_text(yaml.safe_dump(config, sort_keys=False))
    return NO_VERSION


def contract_matches(report: dict) -> bool:
    try:
        validate(report)
        return True
    except (KeyError, TypeError, ValueError):
        return False


def verify_dev_evidence(deployment: dict, baseline: dict, candidate: dict, gate: dict) -> dict:
    """The dev pipeline promoted this exact candidate after a passing gate."""
    evaluated = candidate["provenance"]
    if deployment.get("status") != "promoted" or deployment.get("smoke_passed") is not True:
        raise ValueError("Dev pipeline did not complete promotion and smoke checks")
    if deployment.get("selected_version") != evaluated["version"] \
            or deployment.get("candidate_provenance") != evaluated:
        raise ValueError("Dev deployment record does not match the evaluated candidate")
    if gate.get("passed") is not True or not gate.get("metrics"):
        raise ValueError("Recorded dev gate did not pass")
    if contract_matches(candidate):
        # Same evaluation contract as this checkout: recompute rather than trust the record.
        recomputed = compare(baseline, candidate)
        if not recomputed["passed"]:
            raise ValueError(f"Dev evidence does not pass the release gate: {recomputed['failures']}")
    for key in ("artifact", "toolbox", "toolbox_sha256"):
        if not evaluated.get(key):
            raise ValueError(f"Dev evidence lacks {key}; re-run the dev pipeline")
    return evaluated


def verify_candidate(project, endpoint: str, token: str, agent: str, version: str,
                     baseline_version: str, evaluated: dict) -> dict:
    selection(version)
    if baseline_version == NO_VERSION:
        # First production release: azd routes the agent to @latest. Accept that unpinned state
        # only when @latest is exactly this candidate, then pin it explicitly.
        versions = [item.as_dict().get("version") for item in project.agents.list_versions(agent)]
        if max(versions, key=int) != version:
            raise ValueError(f"Bootstrap expects the candidate to be the newest production version: {versions}")
    elif current_route(project, agent) != baseline_version:
        raise ValueError("Production routing changed during deployment")
    identity = artifact_identity(project, agent, version)
    if identity["artifact"] != evaluated["artifact"]:
        raise ValueError("Production artifact differs from the artifact evaluated in dev")
    if identity["deployed_git_sha"] != source_revision():
        raise ValueError("Production version does not represent the checked-out revision")
    name, toolbox_version = toolbox_reference()
    if f"{name}:{toolbox_version}" != evaluated["toolbox"]:
        raise ValueError("Production agent references a different toolbox version")
    toolbox = toolbox_fingerprint(endpoint, token, name, toolbox_version)
    if toolbox["toolbox_sha256"] != evaluated["toolbox_sha256"]:
        raise ValueError("Production toolbox content differs from the content evaluated in dev")
    return {
        "status": "unchanged" if version == baseline_version else "candidate",
        "agent": agent, "previous_version": baseline_version, "candidate_version": version,
        "identity": identity, "toolbox": toolbox,
        "evaluated_in": {"endpoint": evaluated["endpoint"], "version": evaluated["version"],
                         "git_sha": evaluated["deployed_git_sha"]},
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def serving_artifact(project, agent: str) -> str | None:
    version = current_route(project, agent)
    return None if version == NO_VERSION else artifact_identity(project, agent, version)["artifact"]


def smoke(project, credential, endpoint: str, agent: str, version: str) -> dict:
    answer = invoke(project, credential, endpoint, agent, version,
                    "I'd like to reserve \"The Quantum Library\" by ISBN 978-0-525-55555-5. My patron ID is P20261012.",
                    include_tool_events=False)
    routed = invoke(project, credential, endpoint, agent, version,
                    load_rows()[ROUTING_SMOKE_CASE]["query"])
    tools = [call["name"] for call in routed["tool_calls"]]
    if "f100_public_library_3d1_fine_fees" not in tools:
        raise RuntimeError(f"Routing smoke did not call f100_public_library_3d1_fine_fees (observed: {tools or 'none'})")
    if not routed["tool_definitions"]:
        raise RuntimeError("Production runtime exposed no tools")
    return {"default_stream_characters": len(answer["text"]), "routing_tools": tools,
            "tools_exposed": sorted(tool["name"] for tool in routed["tool_definitions"])}


def unpin(project, agent: str) -> None:
    """Return a never-released agent to azd's default routing so it does not count as released."""
    project.beta.agents.patch_agent_details(agent, body={"agent_endpoint": {
        "protocols": ["invocations"], "version_selector": {"version_selection_rules": [
            {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}]}}})


def promote(project, credential, endpoint: str, agent: str, version: str, previous: str) -> dict:
    pin(project, agent, version)
    try:
        if route(project, agent) != version:
            raise RuntimeError("Production route does not match the promoted version")
        checks = smoke(project, credential, endpoint, agent, version)
    except (AzureError, httpx.HTTPError, RuntimeError, ValueError):
        if previous != NO_VERSION and route(project, agent) == version:
            pin(project, agent, previous)
            print(f"Smoke failed; rolled back {agent} to {previous}")
        elif previous == NO_VERSION:
            unpin(project, agent)
            print(f"Smoke failed on the first release; {agent} is not marked as released")
        raise
    return checks


def output(**values):
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in values.items())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check-serving", "smoke-serving", "prepare", "verify", "promote"])
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--agent", default="public-library-3d1")
    parser.add_argument("--manifest", type=Path, default=Path("agent.yaml"))
    parser.add_argument("--version")
    parser.add_argument("--previous")
    parser.add_argument("--dev-evidence", type=Path, help="Directory with dev deployment/evaluation artifacts")
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    with get_credential() as credential, AIProjectClient(
        endpoint=args.endpoint, credential=credential, allow_preview=True
    ) as project:
        if args.command == "check-serving":
            evaluated = json.loads((args.dev_evidence / "candidate.json").read_text())["provenance"]
            current = serving_artifact(project, args.agent)
            same = current == evaluated["artifact"]
            print(f"Production serves artifact {str(current)[:12]}; promoted dev artifact "
                  f"{evaluated['artifact'][:12]} -> {'already released' if same else 'release needed'}")
            output(release_needed=str(not same).lower())
        elif args.command == "smoke-serving":
            version = current_route(project, args.agent)
            if version == NO_VERSION:
                raise ValueError("Production has no released version to health-check")
            print(f"Health check of serving {args.agent}:v{version}: "
                  f"{smoke(project, credential, args.endpoint, args.agent, version)}")
        elif args.command == "prepare":
            previous = prepare_environment(project, args.agent, args.manifest)
            print(f"Production route during deployment: {previous}")
            output(previous_version=previous)
        elif args.command == "verify":
            if not (args.version and args.previous and args.dev_evidence and args.record):
                parser.error("verify requires --version, --previous, --dev-evidence and --record")
            load = lambda name: json.loads((args.dev_evidence / name).read_text())
            evaluated = verify_dev_evidence(load("deployment.json"), load("baseline.json"),
                                            load("candidate.json"), load("gate.json"))
            token = credential.get_token("https://ai.azure.com/.default").token
            record = verify_candidate(project, args.endpoint, token, args.agent, args.version,
                                      args.previous, evaluated)
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(json.dumps(record, indent=2) + "\n")
            print(f"Verified {args.agent}:{args.version} is dev-evaluated artifact "
                  f"{evaluated['artifact'][:12]} (dev v{evaluated['version']}); status {record['status']}")
            output(changed=str(record["status"] == "candidate").lower())
        else:
            if not (args.version and args.previous and args.record):
                parser.error("promote requires --version, --previous and --record")
            record = json.loads(args.record.read_text())
            if record["candidate_version"] != args.version or record["status"] != "candidate":
                raise ValueError("Promotion requires a verified production candidate record")
            checks = promote(project, credential, args.endpoint, args.agent, args.version, args.previous)
            record.update(status="promoted", selected_version=route(project, args.agent),
                          smoke=checks, promoted_at=datetime.now(timezone.utc).isoformat())
            args.record.write_text(json.dumps(record, indent=2) + "\n")
            print(f"Promoted {args.agent}:{args.version} in production (previous {args.previous}); smoke {checks}")
