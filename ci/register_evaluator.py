"""Register/verify the pinned native evaluators; never overwrite an existing contract."""

import argparse
import json

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import ResourceNotFoundError

from provenance import credential as get_credential
from quality_gate import EVALUATOR, PINNED_EVALUATORS


def pinned_spec(name: str) -> dict:
    for path in PINNED_EVALUATORS:
        spec = json.loads(path.read_text())
        if spec["name"] == name:
            return spec
    raise ValueError(f"Configured evaluator {name} has no pinned source contract")


def verify_evaluator(project, name: str, version: str) -> None:
    spec = pinned_spec(name)
    actual = project.beta.evaluators.get_version(name, version).as_dict()
    if actual["definition"] != spec["definition"]:
        raise ValueError("Registered evaluator definition differs from the pinned source contract")


def register_one(project, spec: dict) -> str:
    try:
        verify_evaluator(project, spec["name"], "1")
        return "1"
    except ResourceNotFoundError:
        created = project.beta.evaluators.create_version(spec["name"], spec).as_dict()
        version = created["version"]
        if version != "1":
            raise ValueError(f"Unexpected evaluator version {version}; review the remote registry")
        verify_evaluator(project, spec["name"], version)
        return version


def register(project) -> str:
    # Foundry Agent Factory: register every pinned evaluator (behavior contract and the use case's rubric).
    versions = [register_one(project, json.loads(path.read_text())) for path in PINNED_EVALUATORS]
    return ",".join(versions)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    args = parser.parse_args()
    with get_credential() as credential, AIProjectClient(
        endpoint=args.endpoint, credential=credential, allow_preview=True
    ) as project:
        print(f"Verified native evaluator version {register(project)}")
