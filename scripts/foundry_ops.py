"""Foundry operations for a Foundry Agent Factory use case: publish skills, create/verify toolbox, grant roles.

Usage (from the generated repository root):
    python scripts/foundry_ops.py publish-skills
    python scripts/foundry_ops.py create-toolbox
    python scripts/foundry_ops.py verify-toolbox
    python scripts/foundry_ops.py grant-roles
A2A (incoming card/protocol, peers, access) lives in scripts/a2a_ops.py; usecase.yaml peers with
transport a2a become `a2a` tools in the toolbox created here (mcp peers are APIM MCP tools, see apim_mcp_ops.py).
Reads usecase.yaml and FOUNDRY_PROJECT_ENDPOINT (from the environment or .env).
Only resources named f100-<id>-* are created; existing resources are never modified.
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import httpx
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS = "Skills=V1Preview"
TOOLBOXES = "Toolboxes=V1Preview"
RPC_ATTEMPTS = 4
TRANSIENT_HTTP_STATUS = {408, 429, 500, 502, 503, 504}
CONTROL_PLANE_ATTEMPTS = 5


def send(http: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """A Foundry control-plane call that survives transient 5xx/429 answers and dropped connections.

    The Skills and Toolboxes preview APIs sometimes answer `500 server_error` once and succeed on the next try;
    a generation or release must not fail on that."""
    for attempt in range(1, CONTROL_PLANE_ATTEMPTS + 1):
        try:
            response = http.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == CONTROL_PLANE_ATTEMPTS:
                raise
            reason = repr(exc)
        else:
            if response.status_code not in TRANSIENT_HTTP_STATUS or attempt == CONTROL_PLANE_ATTEMPTS:
                return response
            reason = f"HTTP {response.status_code}"
        delay = 2 ** attempt
        print(f"{method} {url}: transient failure ({reason}); retrying in {delay}s "
              f"({attempt}/{CONTROL_PLANE_ATTEMPTS})", file=sys.stderr, flush=True)
        time.sleep(delay)
    raise AssertionError("unreachable")


def load_env() -> None:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    if (ROOT / ".azure").is_dir():
        result = subprocess.run(["azd", "env", "get-values"], cwd=ROOT, capture_output=True, text=True)
        for line in result.stdout.splitlines() if result.returncode == 0 else []:
            key, _, value = line.partition("=")
            if key and value:
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


def spec() -> dict:
    return yaml.safe_load((ROOT / "usecase.yaml").read_text())


def names(s: dict) -> dict:
    prefix = f"f100-{s['id']}"
    return {
        # New agents are named after their id; repositories generated before agent_name existed keep the f100- prefix.
        "agent": s.get("agent_name") or prefix,
        "toolbox": f"{prefix}-toolbox",
        "skills": [f"{prefix}-{skill['name']}" for skill in s["skills"]],
    }


def endpoint() -> str:
    value = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not value.startswith("https://"):
        sys.exit("FOUNDRY_PROJECT_ENDPOINT is not set (see .env.example)")
    return value


def token() -> str:
    return subprocess.check_output(
        ["az", "account", "get-access-token", "--resource", "https://ai.azure.com", "--query", "accessToken", "-o", "tsv"],
        text=True,
    ).strip()


def client(feature: str) -> httpx.Client:
    return httpx.Client(
        base_url=endpoint(),
        params={"api-version": "v1"},
        headers={"Authorization": f"Bearer {token()}", "Foundry-Features": feature, "Accept": "application/json"},
        timeout=60,
    )


def split_skill(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError("SKILL.md must start with YAML frontmatter")
    return yaml.safe_load(match.group(1)) or {}, match.group(2).strip()


def publish_skills(force: bool) -> None:
    with client(SKILLS) as http:
        for name in names(spec())["skills"]:
            meta, body = split_skill((ROOT / "toolbox-skills" / name / "SKILL.md").read_text())
            existing = send(http, "GET", f"/skills/{name}")
            if existing.status_code == 200 and not force:
                print(f"skill {name}: exists (default v{existing.json().get('default_version')}), skipped")
                continue
            response = send(
                http, "POST", f"/skills/{name}/versions",
                json={"inline_content": {"description": meta["description"], "instructions": body}},
            )
            if response.status_code >= 300:
                sys.exit(f"skill {name}: HTTP {response.status_code} {response.text[:300]}")
            print(f"skill {name}: published v{response.json().get('version')}")


def a2a_connection_name(s: dict, peer: dict) -> str:
    return f"f100-{s['id']}-a2a-{peer['name']}"


def peer_target(peer: dict, project_endpoint: str) -> str:
    if peer.get("agent"):
        return f"{project_endpoint.rstrip('/')}/agents/{peer['agent']}/endpoint/protocols/a2a"
    return peer["url"].rstrip("/")


def a2a_peers(s: dict) -> list[dict]:
    """Peers reached over A2A (toolbox `a2a` tool); mcp peers are APIM MCP tools instead."""
    return [peer for peer in s.get("peers") or [] if peer.get("transport", "mcp") == "a2a"]


def a2a_tools(s: dict, connection_id=None) -> list[dict]:
    """Outgoing A2A: one GA `a2a` (v1.0) toolbox tool per peer in usecase.yaml a2a.peers."""
    connection_id = connection_id or project_connection_id
    return [
        {
            "type": "a2a",
            "a2a_version": "1.0",
            "name": peer["name"].replace("-", "_"),
            "description": peer["description"],
            "base_url": peer_target(peer, endpoint()),
            "project_connection_id": connection_id(a2a_connection_name(s, peer)),
        }
        for peer in a2a_peers(s)
    ]


def project_connection_id(connection_name: str) -> str:
    project_endpoint = httpx.URL(endpoint())
    account_name = (project_endpoint.host or "").split(".", 1)[0]
    project_name = project_endpoint.path.rstrip("/").rsplit("/", 1)[-1]
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip() or subprocess.check_output(
        ["az", "account", "show", "--query", "id", "-o", "tsv"], text=True,
    ).strip()
    resource_group = os.environ.get("FOUNDRY_PROJECT_RESOURCE_GROUP", "").strip() or subprocess.check_output(
        [
            "az", "cognitiveservices", "account", "list",
            "--query", f"[?name=='{account_name}'] | [0].resourceGroup", "-o", "tsv",
        ],
        text=True,
    ).strip()
    if not subscription_id or not resource_group:
        sys.exit(f"could not resolve the Foundry resource ID for {account_name}/{project_name}")
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}"
        f"/projects/{project_name}/connections/{connection_name}"
    )


def create_toolbox() -> None:
    n = names(spec())
    toolbox = yaml.safe_load((ROOT / "toolbox.yaml").read_text())
    manifest = yaml.safe_load((ROOT / "api" / "apim-manifest.yaml").read_text())
    connection_name = manifest["mcp_server"]["connection"]
    gateway_url = os.environ.get("AZURE_API_MANAGEMENT_GATEWAY_URL", "").rstrip("/")
    if not gateway_url.startswith("https://"):
        sys.exit("AZURE_API_MANAGEMENT_GATEWAY_URL is not set (see .env.example)")
    payload = {
        "description": toolbox["description"],
        "tools": [{
            "type": "mcp",
            "name": connection_name,
            "server_label": connection_name,
            "server_url": f"{gateway_url}/{manifest['mcp_server']['path']}/mcp",
            "project_connection_id": project_connection_id(connection_name),
        }, *a2a_tools(spec())],
        "skills": [
            {"type": "skill_reference", "name": skill["name"]}
            for skill in toolbox["skills"]
        ],
    }
    with client(TOOLBOXES) as http:
        created = send(http, "POST", f"/toolboxes/{n['toolbox']}/versions", json=payload)
        if created.status_code >= 300:
            sys.exit(f"toolbox {n['toolbox']}: HTTP {created.status_code} {created.text[:500]}")
        version = str(created.json().get("version") or "")
        if not version:
            sys.exit(f"toolbox {n['toolbox']}: version create response did not include a version")
        published = send(http, "PATCH", f"/toolboxes/{n['toolbox']}", json={"default_version": version})
        if published.status_code >= 300:
            sys.exit(
                f"toolbox {n['toolbox']} publish v{version}: "
                f"HTTP {published.status_code} {published.text[:500]}"
            )
    print(f"toolbox {n['toolbox']}: published v{version}")


def _rpc(http: httpx.Client, url: str, method: str, params: dict | None, session: str | None, rpc_id: int):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, **({"params": params} if params is not None else {})}
    for attempt in range(1, RPC_ATTEMPTS + 1):
        try:
            response = http.post(url, json=body, headers=headers)
            response.raise_for_status()
            break
        except httpx.TransportError as exc:
            retryable = True
            error = exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in TRANSIENT_HTTP_STATUS
            error = exc
        if not retryable or attempt == RPC_ATTEMPTS:
            raise error
        delay = 2 ** (attempt - 1)
        print(f"{method}: transient MCP failure ({error}); retrying in {delay}s "
              f"({attempt}/{RPC_ATTEMPTS})", file=sys.stderr, flush=True)
        time.sleep(delay)
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        text = "\n".join(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text), response.headers.get("Mcp-Session-Id") or session


def verify_toolbox(version: str | None) -> None:
    n = names(spec())
    url = (
        f"/toolboxes/{n['toolbox']}/versions/{version}/mcp"
        if version
        else f"/toolboxes/{n['toolbox']}/mcp"
    )
    with client(TOOLBOXES) as http:
        init, session = _rpc(http, url, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "foundry100-verify", "version": "1"}}, None, 1)
        if "error" in init:
            sys.exit(f"initialize failed: {init['error']}")
        listed, session = _rpc(http, url, "resources/list", {}, session, 2)
        tools, _ = _rpc(http, url, "tools/list", {}, session, 3)
    uris = {r.get("uri") for r in (listed.get("result") or {}).get("resources", [])}
    expected = {f"skill://{skill}/SKILL.md" for skill in n["skills"]}
    missing = sorted(expected - uris)
    tool_names = {tool.get("name") for tool in (tools.get("result") or {}).get("tools", [])}
    expected_tools = {operation["tool_name"] for operation in spec()["mcp"]["operations"]}
    discovered_tools = {name.rsplit("___", 1)[-1] for name in tool_names if name}
    missing_tools = sorted(expected_tools - discovered_tools)
    print(json.dumps({
        "toolbox": n["toolbox"], "version": version or "default",
        "resources": sorted(u for u in uris if u),
        "tools": sorted(name for name in tool_names if name),
    }, indent=2))
    if missing:
        sys.exit(f"missing skill resources: {missing}")
    if missing_tools:
        sys.exit(f"missing MCP tools: {missing_tools}")
    print("toolbox verified: all skill resources and MCP tools present")


def agent_principal(agent: str) -> str:
    with client(TOOLBOXES) as http:
        response = http.get(f"/agents/{agent}")
    response.raise_for_status()
    data = response.json()
    principal = (data.get("instance_identity") or {}).get("principal_id") or (
        ((data.get("versions") or {}).get("latest") or {}).get("instance_identity") or {}
    ).get("principal_id")
    if not principal:
        sys.exit("agent has no instance_identity yet (deploy it first)")
    return principal


def grant_roles(principal: str | None) -> None:
    n = names(spec())
    principal = principal or agent_principal(n["agent"])
    project_id = os.environ.get("AZURE_AI_PROJECT_ID")
    if not project_id:
        sys.exit("AZURE_AI_PROJECT_ID is not set (ARM id of the project)")
    account_id = project_id.split("/projects/")[0]
    assignments = (
        ("Foundry User", project_id),
        ("Cognitive Services User", account_id),
        ("Cognitive Services OpenAI User", account_id),
    )
    for role, scope in assignments:
        command = ["az", "role", "assignment", "create", "--assignee-object-id", principal,
                   "--assignee-principal-type", "ServicePrincipal", "--role", role, "--scope", scope, "-o", "none"]
        for attempt in range(1, 4):
            try:  # an Azure CLI call can hang on a stale connection; bound it and try again (the call is idempotent)
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
                returncode, detail = result.returncode, result.stderr.strip()[:300]
            except subprocess.TimeoutExpired:
                returncode, detail = 1, "timed out after 120s"
            if returncode == 0 or attempt == 3:
                break
            print(f"role {role}: attempt {attempt} failed ({detail}); retrying")
        print(f"role {role} on {scope.split('/')[-1]} for {principal}: {'ok' if returncode == 0 else 'failed: ' + detail}")
        if returncode != 0:
            sys.exit(1)


def pin_evaluators(endpoint: str) -> None:
    """Register the pinned CI evaluators if missing and pin the service-normalized definitions.

    Foundry adds defaults (for example data_schema and metrics for rubric evaluators); the pipeline's
    verify step requires the registered definition to equal the pinned file, so the file is updated
    to exactly what the service stored.
    """
    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import AzureCliCredential

    files = [ROOT / "ci" / "expected_behavior_evaluator.json", ROOT / "ci" / "domain_rubric_evaluator.json"]
    with AIProjectClient(endpoint=endpoint, credential=AzureCliCredential(), allow_preview=True,
                         connection_timeout=30, read_timeout=120) as project:
        for path in files:
            if not path.exists():
                continue
            spec = json.loads(path.read_text())
            try:
                actual = project.beta.evaluators.get_version(spec["name"], "1").as_dict()
                state = "exists"
            except ResourceNotFoundError:
                actual = project.beta.evaluators.create_version(spec["name"], spec).as_dict()
                state = f"created v{actual.get('version')}"
                if actual.get("version") != "1":
                    sys.exit(f"{spec['name']}: unexpected version {actual.get('version')}")
            if actual["definition"] != spec["definition"]:
                spec["definition"] = actual["definition"]
                path.write_text(json.dumps(spec, indent=2) + "\n")
                state += ", pinned normalized definition"
            print(f"evaluator {spec['name']}: {state}")


def pinned_route(endpoint: str, agent: str) -> str:
    """Numeric version the agent endpoint is pinned to, '@latest', or 'none' if the agent does not exist."""
    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import AzureCliCredential

    with AIProjectClient(endpoint=endpoint, credential=AzureCliCredential(), allow_preview=True,
                         connection_timeout=30, read_timeout=120) as project:
        try:
            data = project.agents.get(agent).as_dict()
        except ResourceNotFoundError:
            return "none"
    rules = ((data.get("agent_endpoint") or {}).get("version_selector") or {}).get("version_selection_rules") or []
    if len(rules) == 1 and str(rules[0].get("agent_version", "")).isdigit():
        return str(rules[0]["agent_version"])
    return "@latest"


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish-skills")
    publish.add_argument("--force", action="store_true", help="publish a new version even if the skill exists")
    sub.add_parser("create-toolbox")
    verify = sub.add_parser("verify-toolbox")
    verify.add_argument("--version", default=None)
    pin = sub.add_parser("pin-evaluators")
    pin.add_argument("--endpoint", default=None, help="project endpoint (default FOUNDRY_PROJECT_ENDPOINT)")
    sub.add_parser("route", help="print the pinned version of the f100 agent in FOUNDRY_PROJECT_ENDPOINT")
    grant = sub.add_parser("grant-roles")
    grant.add_argument("--principal", help="agent identity object id (auto-detected when omitted)")
    args = parser.parse_args()
    if args.command == "publish-skills":
        publish_skills(args.force)
    elif args.command == "create-toolbox":
        create_toolbox()
    elif args.command == "verify-toolbox":
        verify_toolbox(args.version)
    elif args.command == "route":
        print(pinned_route(endpoint(), names(spec())["agent"]))
    elif args.command == "pin-evaluators":
        pin_evaluators(args.endpoint or endpoint())
    else:
        grant_roles(args.principal)


if __name__ == "__main__":
    main()
