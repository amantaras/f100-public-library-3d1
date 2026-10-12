"""Release identity and deterministic CI authentication."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

from azure.identity import AzureCliCredential, DefaultAzureCredential


def credential():
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return AzureCliCredential()
    return DefaultAzureCredential()


def toolbox_reference(manifest: Path | None = None) -> tuple[str, str]:
    """Toolbox name/version the agent manifest configures."""
    import yaml

    manifest = manifest or Path(__file__).resolve().parents[1] / "agent.yaml"
    values = {item["name"]: str(item["value"])
              for item in yaml.safe_load(manifest.read_text()).get("environment_variables", [])}
    return values["TOOLBOX_NAME"], values["TOOLBOX_VERSION"]


def toolbox_fingerprint(endpoint: str, token: str, name: str, version: str) -> dict:
    """SHA-256 over every resource the toolbox MCP endpoint serves (the agent's external guidance)."""
    import httpx

    url = f"{endpoint.rstrip('/')}/toolboxes/{name}/versions/{version}/mcp?api-version=v1"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "Foundry-Features": "Toolboxes=V1Preview"}

    def call(client, request_id, method, params=None):
        response = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"Toolbox {method} failed: {payload['error'].get('message')}")
        return response, payload["result"]

    with httpx.Client(timeout=60) as client:
        response, _ = call(client, 1, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "ci-toolbox-fingerprint", "version": "1"}})
        if response.headers.get("mcp-session-id"):
            headers["mcp-session-id"] = response.headers["mcp-session-id"]
        _, listed = call(client, 2, "resources/list")
        uris = sorted(item["uri"] for item in listed.get("resources", []))
        if not uris:
            raise RuntimeError("Toolbox serves no resources")
        digest = hashlib.sha256()
        for index, uri in enumerate(uris, start=3):
            _, read = call(client, index, "resources/read", {"uri": uri})
            text = "".join(content.get("text") or "" for content in read.get("contents", []))
            digest.update(uri.encode() + b"\0" + text.encode() + b"\0")
    return {"toolbox": f"{name}:{version}", "toolbox_resources": uris, "toolbox_sha256": digest.hexdigest()}


def source_revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], text=True
    ).strip()


def artifact_identity(project, agent: str, version: str) -> dict:
    deployed = project.agents.get_version(agent, version).as_dict()
    if deployed.get("status") != "active":
        raise ValueError(f"{agent}:{version} is not active")
    definition = deployed["definition"]
    artifact = (definition.get("code_configuration") or {}).get("content_hash") or definition.get("image")
    if not artifact:
        raise ValueError("Agent version has no immutable deployment artifact identity")
    return {
        "artifact": artifact,
        "deployed_git_sha": deployed.get("metadata", {}).get("git_sha"),
        "definition_sha256": hashlib.sha256(
            json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
