"""Publish and verify a Foundry Agent Factory REST API and APIM-hosted MCP server."""
import argparse
import base64
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
API_VERSION = "2025-09-01-preview"
TRANSIENT = {408, 429, 500, 502, 503, 504}
CONSUMER_ROLE_ID = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"  # Foundry Agent Consumer
LIST_ATTEMPTS = 8  # publish-mcp returns before every tool is visible
LIST_DELAY = 10
PEER_SMOKE_ATTEMPTS = 8  # role assignments take minutes to propagate


def load_env() -> None:
    for env_file in (ROOT / ".env",):
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip('"'))
    if (ROOT / ".azure").is_dir():
        result = subprocess.run(
            ["azd", "env", "get-values"], cwd=ROOT, capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                key, _, value = line.partition("=")
                if key and value:
                    os.environ.setdefault(key.strip(), value.strip().strip('"'))


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is required")
    return value


def manifest() -> dict:
    return yaml.safe_load((ROOT / "api" / "apim-manifest.yaml").read_text())


def mcp_audience() -> str:
    value = required("AZURE_APIM_MCP_AUDIENCE")
    return value if value.startswith("api://") else f"api://{value}"


def project_principal_id() -> str:
    configured = os.environ.get("FOUNDRY_PROJECT_PRINCIPAL_ID", "").strip()
    if configured:
        return configured
    project_endpoint = httpx.URL(required("FOUNDRY_PROJECT_ENDPOINT"))
    account_name = (project_endpoint.host or "").split(".", 1)[0]
    project_name = project_endpoint.path.rstrip("/").rsplit("/", 1)[-1]
    resource_group = os.environ.get("FOUNDRY_PROJECT_RESOURCE_GROUP", "").strip()
    if not resource_group:
        resource_group = subprocess.check_output(
            [
                "az", "cognitiveservices", "account", "list",
                "--query", f"[?name=='{account_name}'] | [0].resourceGroup", "-o", "tsv",
            ],
            text=True,
        ).strip()
    if not resource_group:
        sys.exit(f"Foundry account {account_name} was not found in the selected subscription")
    url = (
        "https://management.azure.com/subscriptions/"
        f"{required('AZURE_SUBSCRIPTION_ID')}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}/projects/{project_name}"
        "?api-version=2025-06-01"
    )
    with httpx.Client(
        headers={"Authorization": f"Bearer {management_token()}"},
        timeout=120,
    ) as client:
        response = _request(client, "GET", url)
    principal = ((response.json().get("identity") or {}).get("principalId") or "").strip()
    if not principal:
        sys.exit(f"Foundry project {project_name} does not expose a system-assigned identity")
    return principal


def token_claim(token: str, name: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    value = json.loads(base64.urlsafe_b64decode(payload)).get(name, "")
    if not value:
        sys.exit(f"Azure management token does not contain the {name} claim")
    return value


def rendered_policy(name: str) -> str:
    token = management_token()
    values = {
        "__F100_TENANT_ID__": os.environ.get("AZURE_TENANT_ID", "").strip()
        or token_claim(token, "tid"),
        "__F100_MCP_AUDIENCE_ID__": mcp_audience().removeprefix("api://"),
        "__F100_PROJECT_PRINCIPAL_ID__": project_principal_id(),
        "__F100_DEPLOYER_PRINCIPAL_ID__": token_claim(token, "oid"),
        "__F100_PROJECT_ENDPOINT__": required("FOUNDRY_PROJECT_ENDPOINT").rstrip("/"),
    }
    policy = (ROOT / "api" / "policies" / name).read_text()
    for marker, value in values.items():
        policy = policy.replace(marker, value)
    if "__F100_" in policy:
        sys.exit(f"{name} contains an unresolved Foundry Agent Factory policy marker")
    return policy


def management_token() -> str:
    return subprocess.check_output(
        [
            "az", "account", "get-access-token",
            "--resource", "https://management.azure.com",
            "--query", "accessToken", "-o", "tsv",
        ],
        text=True,
    ).strip()


def arm_client() -> tuple[httpx.Client, str]:
    subscription = required("AZURE_SUBSCRIPTION_ID")
    resource_group = required("AZURE_API_MANAGEMENT_RESOURCE_GROUP")
    apim_name = required("AZURE_API_MANAGEMENT_NAME")
    base = (
        f"https://management.azure.com/subscriptions/{subscription}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{apim_name}"
    )
    client = httpx.Client(
        headers={"Authorization": f"Bearer {management_token()}", "Content-Type": "application/json"},
        timeout=240,
    )
    return client, base


def _request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    for attempt in range(1, 5):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == 4:
                sys.exit(f"APIM {method} failed after transport retries: {exc}")
            time.sleep(2 ** (attempt - 1))
            continue
        if response.status_code < 300:
            return response
        if response.status_code not in TRANSIENT or attempt == 4:
            sys.exit(f"APIM {method} failed: HTTP {response.status_code} {response.text[:500]}")
        time.sleep(2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _put(client: httpx.Client, url: str, payload: dict) -> httpx.Response:
    response = _request(client, "PUT", url, headers={"If-Match": "*"}, json=payload)
    operation_url = response.headers.get("Azure-AsyncOperation") or response.headers.get("Location")
    if operation_url:
        for _ in range(120):
            poll = _request(client, "GET", operation_url)
            state = (poll.json().get("status") or "").lower()
            if state in {"succeeded", "completed"}:
                break
            if state in {"failed", "canceled", "cancelled"}:
                sys.exit(f"APIM async operation failed: {poll.text[:500]}")
            time.sleep(2)
        else:
            sys.exit("APIM async operation did not complete within four minutes")
    return response


def publish_api() -> None:
    data = manifest()
    openapi = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text())
    policy = rendered_policy("api-policy.xml")
    api_id = data["rest_api"]["name"]
    client, base = arm_client()
    with client:
        _put(
            client,
            f"{base}/apis/{api_id}?api-version={API_VERSION}",
            {
                "properties": {
                    "displayName": openapi["info"]["title"],
                    "description": openapi["info"]["description"],
                    "path": data["rest_api"]["path"],
                    "protocols": ["https"],
                    "subscriptionRequired": False,
                    "type": "http",
                    "format": "openapi+json",
                    "value": json.dumps(openapi),
                }
            },
        )
        _put(
            client,
            f"{base}/apis/{api_id}/policies/policy?api-version={API_VERSION}",
            {"properties": {"format": "rawxml", "value": policy}},
        )
        for peer in data.get("peers", []):
            _put(
                client,
                f"{base}/apis/{api_id}/operations/{peer['operation_id']}/policies/policy?api-version={API_VERSION}",
                {"properties": {"format": "rawxml", "value": rendered_policy(pathlib.Path(peer["policy"]).name)}},
            )
    mocked = len(data["tools"]) - len(data.get("peers", []))
    print(f"REST API {api_id}: published with {mocked} mocked operations and {len(data.get('peers', []))} peer operations")


def extend_oid_policy(xml: str, principal: str) -> str | None:
    """`xml` with `principal` added to the allowed `oid` claim values; None when it is already allowed.

    The MCP and REST policies admit only the Foundry project identities listed in their `oid` claim. Another project of the
    same account (PROD next to DEV) calls the same gateway with its own identity, so it must be added, never substituted."""
    if principal.lower() in xml.lower():
        return None
    extended, count = re.subn(r'(<claim name="oid" match="any">)', rf"\1<value>{principal}</value>", xml, count=1)
    if not count:
        sys.exit("the policy has no oid claim to extend; republish it (publish-api / publish-mcp) first")
    return extended


def authorize_project() -> None:
    """Allow this project's identity (FOUNDRY_PROJECT_ENDPOINT) through the MCP and REST policies; idempotent and additive."""
    principal = project_principal_id()
    data = manifest()
    client, base = arm_client()
    changed = []
    with client:
        for api_id in (data["mcp_server"]["name"], data["rest_api"]["name"]):
            url = f"{base}/apis/{api_id}/policies/policy?api-version={API_VERSION}"
            # ARM answers this GET with the raw policy XML (not JSON), prefixed by a byte-order mark.
            current = _request(client, "GET", f"{url}&format=rawxml").text.lstrip("\ufeff")
            extended = extend_oid_policy(current, principal)
            if extended is None:
                continue
            _put(client, url, {"properties": {"format": "rawxml", "value": extended}})
            changed.append(api_id)
    print(f"project identity {principal}: " + (f"authorized on {', '.join(changed)}" if changed else "already authorized"))


def publish_mcp() -> None:
    data = manifest()
    mcp_id = data["mcp_server"]["name"]
    api_id = data["rest_api"]["name"]
    product_id = data["product"]
    subscription_id = data["subscription"]
    policy = rendered_policy("mcp-policy.xml")
    client, base = arm_client()
    with client:
        _put(
            client,
            f"{base}/apis/{mcp_id}?api-version={API_VERSION}",
            {
                "properties": {
                    "type": "mcp",
                    "path": data["mcp_server"]["path"],
                    "displayName": f"{mcp_id} MCP server",
                    "description": "Foundry Agent Factory APIM-hosted MCP tools",
                    "protocols": ["https"],
                    "subscriptionRequired": False,
                }
            },
        )
        for tool in data["tools"]:
            operation_id = f"{base}/apis/{api_id}/operations/{tool['operation_id']}"
            _put(
                client,
                f"{base}/apis/{mcp_id}/tools/{tool['id']}?api-version={API_VERSION}",
                {
                    "properties": {
                        "displayName": tool["display_name"],
                        "description": tool["description"],
                        "operationId": operation_id.replace("https://management.azure.com", ""),
                    }
                },
            )
        _put(
            client,
            f"{base}/apis/{mcp_id}/policies/policy?api-version={API_VERSION}",
            {"properties": {"format": "rawxml", "value": policy}},
        )
        _put(
            client,
            f"{base}/products/{product_id}?api-version={API_VERSION}",
            {
                "properties": {
                    "displayName": product_id,
                    "description": "Foundry Agent Factory MCP access",
                    "subscriptionRequired": True,
                    "approvalRequired": False,
                    "state": "published",
                }
            },
        )
        _put(
            client,
            f"{base}/products/{product_id}/apis/{mcp_id}?api-version={API_VERSION}",
            {},
        )
        _put(
            client,
            f"{base}/subscriptions/{subscription_id}?api-version={API_VERSION}",
            {
                "properties": {
                    "displayName": subscription_id,
                    "scope": f"/products/{product_id}",
                    "state": "active",
                }
            },
        )
    print(f"MCP server {mcp_id}: published with {len(data['tools'])} tools")


def _subscription_key() -> str:
    data = manifest()
    client, base = arm_client()
    with client:
        response = _request(
            client,
            "POST",
            f"{base}/subscriptions/{data['subscription']}/listSecrets?api-version={API_VERSION}",
            content=b"",
        )
    key = response.json().get("primaryKey")
    if not key:
        sys.exit("APIM subscription did not return a primary key")
    return key


def mcp_url() -> str:
    gateway = os.environ.get("AZURE_API_MANAGEMENT_GATEWAY_URL", "").rstrip("/")
    if not gateway:
        gateway = f"https://{required('AZURE_API_MANAGEMENT_NAME')}.azure-api.net"
    return f"{gateway}/{manifest()['mcp_server']['path']}/mcp"


# azd prints this when the selected azd environment names another project than --project-endpoint. It is informational
# (the command still works) and it must never be mistaken for the failure.
AZD_NOISE = ("does not match resolved endpoint",)


def azd_details(result: subprocess.CompletedProcess) -> str:
    """The text that explains a failed azd call. azd writes its errors to stdout and informational notes to stderr."""
    lines = [line for stream in (result.stdout, result.stderr) for line in (stream or "").splitlines()
             if line.strip() and not any(noise in line for noise in AZD_NOISE)]
    return "\n".join(lines).strip()


def existing_connection(project_endpoint: str, name: str) -> dict | None:
    """The connection called `name` as seen from this project, or None.

    Connections live at the Foundry account, so DEV and PROD projects of one account see the same ones, and only the
    project that created a connection may update it."""
    result = subprocess.run(["azd", "ai", "connection", "list", "--no-prompt", "--project-endpoint", project_endpoint, "-o", "json"],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"Foundry connection list failed: {azd_details(result)[-1000:]}")
    try:
        return next((c for c in json.loads(result.stdout) if c.get("name") == name), None)
    except (json.JSONDecodeError, TypeError):
        sys.exit(f"Foundry connection list returned unreadable output: {result.stdout[-300:]}")


def create_foundry_connection() -> None:
    project_endpoint = required("FOUNDRY_PROJECT_ENDPOINT")
    data = manifest()
    connection_name = data["mcp_server"]["connection"]
    target = mcp_url()
    current = existing_connection(project_endpoint, connection_name)
    if current and (current.get("target") or "").rstrip("/") == target.rstrip("/"):
        # Visible from this project means shared with it: only the creating project may update the connection, so do not try.
        print(f"Foundry connection {connection_name} already exists with the same target; reusing it")
        return
    result = subprocess.run(
        [
            "azd", "ai", "connection", "create", connection_name,
            "--kind", "remote-tool",
            "--target", target,
            "--auth-type", "project-managed-identity",
            "--audience", mcp_audience(),
            "--force", "--no-prompt", "--project-endpoint", project_endpoint, "-o", "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        hint = (" It exists with a different target and another project owns it; delete it or update it from the project that "
                "created it." if current else "")
        sys.exit(f"Foundry connection create failed: {azd_details(result)[-1000:]}{hint}")
    endpoint = httpx.URL(project_endpoint)
    account_name = (endpoint.host or "").split(".", 1)[0]
    project_name = endpoint.path.rstrip("/").rsplit("/", 1)[-1]
    connection_url = (
        "https://management.azure.com/subscriptions/"
        f"{required('AZURE_SUBSCRIPTION_ID')}/resourceGroups/{required('AZURE_RESOURCE_GROUP')}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}/projects/{project_name}"
        f"/connections/{connection_name}?api-version=2025-06-01"
    )
    with httpx.Client(
        headers={"Authorization": f"Bearer {management_token()}", "Content-Type": "application/json"},
        timeout=120,
    ) as client:
        _request(
            client,
            "PATCH",
            connection_url,
            json={
                "properties": {
                    "authType": "ProjectManagedIdentity",
                    "category": "RemoteTool",
                    "target": mcp_url(),
                    "isSharedToAll": True,
                }
            },
        )
    print(f"Foundry connection {connection_name}: upserted and shared with the project")


def apim_principal_id() -> str:
    client, base = arm_client()
    with client:
        response = _request(client, "GET", f"{base}?api-version={API_VERSION}")
    principal = ((response.json().get("identity") or {}).get("principalId") or "").strip()
    if not principal:
        sys.exit("API Management has no system-assigned identity; enable it so it can call peer agents")
    return principal


def _project_scope() -> str:
    endpoint = httpx.URL(required("FOUNDRY_PROJECT_ENDPOINT"))
    account_name = (endpoint.host or "").split(".", 1)[0]
    project_name = endpoint.path.rstrip("/").rsplit("/", 1)[-1]
    return (
        f"/subscriptions/{required('AZURE_SUBSCRIPTION_ID')}/resourceGroups/{required('AZURE_RESOURCE_GROUP')}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}/projects/{project_name}"
    )


def grant_peer_access() -> None:
    """Give API Management's identity Foundry Agent Consumer on each peer agent (least privilege: that agent only)."""
    peers = manifest().get("peers", [])
    if not peers:
        print("no peer agents in this use case; nothing to grant")
        return
    principal = apim_principal_id()
    endpoint = required("FOUNDRY_PROJECT_ENDPOINT").rstrip("/")
    token = subprocess.check_output(
        ["az", "account", "get-access-token", "--resource", "https://ai.azure.com", "--query", "accessToken", "-o", "tsv"],
        text=True).strip()
    for peer in peers:
        found = httpx.get(f"{endpoint}/agents/{peer['agent']}", params={"api-version": "v1"}, timeout=60,
                          headers={"Authorization": f"Bearer {token}"})
        if found.status_code == 404:
            sys.exit(f"peer agent {peer['agent']} does not exist in {endpoint}; deploy it first")
        found.raise_for_status()
        scope = f"{_project_scope()}/agents/{peer['agent']}"
        result = subprocess.run(
            ["az", "role", "assignment", "create", "--assignee-object-id", principal,
             "--assignee-principal-type", "ServicePrincipal", "--role", CONSUMER_ROLE_ID, "--scope", scope,
             "-o", "none"], capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit(f"role assignment for peer {peer['agent']} failed: {result.stderr.strip()[:400]}")
        print(f"APIM identity {principal}: Foundry Agent Consumer on agent {peer['agent']}")


def _rpc(client: httpx.Client, method: str, params: dict | None, rpc_id: int, session: str | None):
    headers = {
        "Authorization": f"Bearer {management_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        payload["params"] = params
    for attempt in range(1, 9):
        try:
            response = client.post(mcp_url(), headers=headers, json=payload)
            response.raise_for_status()
            break
        except httpx.TransportError as exc:
            retryable = True
            error = exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in {401, 404, *TRANSIENT}
            error = exc
        if not retryable or attempt == 8:
            raise error
        time.sleep(min(2 ** (attempt - 1), 15))
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        text = "\n".join(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text), response.headers.get("Mcp-Session-Id") or session


def tool_result_problem(result: dict, is_peer: bool) -> str | None:
    """Why an MCP tool result is not a success, or None. APIM reports backend failures as text, not isError."""
    text = "\n".join(item.get("text", "") for item in result.get("content") or [] if isinstance(item, dict))
    if result.get("isError"):
        return f"isError: {text[:300]}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None if not is_peer else f"peer reply is not JSON: {text[:200]}"
    if isinstance(payload, dict) and isinstance(payload.get("statusCode"), int) and payload["statusCode"] >= 400:
        return f"backend returned HTTP {payload['statusCode']}: {text[:200]}"
    if is_peer and not (isinstance(payload, dict) and str(payload.get("answer") or "").strip()):
        return f"peer returned no answer: {text[:200]}"
    return None


def verify(call_smoke: bool) -> None:
    data = manifest()
    with httpx.Client(timeout=90) as client:
        initialized, session = _rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "foundry-agent-factory", "version": "1"},
            },
            1,
            None,
        )
        if "error" in initialized:
            sys.exit(f"MCP initialize failed: {initialized['error']}")
        expected = {tool["display_name"] for tool in data["tools"]}
        for attempt in range(1, LIST_ATTEMPTS + 1):
            listed, session = _rpc(client, "tools/list", {}, 2, session)
            names = {tool["name"] for tool in (listed.get("result") or {}).get("tools", [])}
            missing = sorted(expected - names)
            if not missing:
                break
            if attempt == LIST_ATTEMPTS:
                sys.exit(f"MCP tools/list missing {missing}")
            # A freshly published MCP server can list only some tools for a few seconds.
            print(f"  tools/list missing {missing}; retrying in {LIST_DELAY}s ({attempt}/{LIST_ATTEMPTS})", flush=True)
            time.sleep(LIST_DELAY)
        if call_smoke:
            smoke = data["smoke"]
            peer_tools = {peer["tool"] for peer in data.get("peers", [])}
            attempts = PEER_SMOKE_ATTEMPTS if smoke["expected_tool"] in peer_tools else 1
            for attempt in range(1, attempts + 1):
                called, _ = _rpc(
                    client,
                    "tools/call",
                    {"name": smoke["expected_tool"], "arguments": smoke["arguments"]},
                    3,
                    session,
                )
                if "error" in called:
                    sys.exit(f"MCP tools/call failed: {called['error']}")
                problem = tool_result_problem(called.get("result") or {}, smoke["expected_tool"] in peer_tools)
                if not problem:
                    break
                if attempt == attempts:
                    sys.exit(f"MCP tool {smoke['expected_tool']} failed: {problem}")
                print(f"  {smoke['expected_tool']} not ready ({problem}); retrying in 30s "
                      f"({attempt}/{attempts})", flush=True)
                time.sleep(30)
    print(f"MCP verified: {len(expected)} tools" + (" and smoke call" if call_smoke else ""))


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("publish-api")
    sub.add_parser("publish-mcp")
    sub.add_parser("create-foundry-connection")
    sub.add_parser("grant-peer-access", help="let API Management call the peer agents")
    sub.add_parser("authorize-project", help="allow this project's identity through the MCP and REST policies (PROD)")
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--call-smoke", action="store_true")
    args = parser.parse_args()
    if args.command == "publish-api":
        publish_api()
    elif args.command == "publish-mcp":
        publish_mcp()
    elif args.command == "create-foundry-connection":
        create_foundry_connection()
    elif args.command == "grant-peer-access":
        grant_peer_access()
    elif args.command == "authorize-project":
        authorize_project()
    else:
        verify(args.call_smoke)


if __name__ == "__main__":
    main()
