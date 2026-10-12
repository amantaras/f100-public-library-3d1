"""Agent2Agent (A2A) operations for a Foundry Agent Factory use case.

Usage (from the generated repository root):
    python scripts/a2a_ops.py enable          # publish the agent card + a2a protocol, verify card and message
    python scripts/a2a_ops.py verify          # verify the published card and send a test message
    python scripts/a2a_ops.py connect-peers   # RemoteA2A connections for usecase.yaml peers with transport a2a
    python scripts/a2a_ops.py grant-access    # let this agent call its Foundry peers (Foundry Agent Consumer)
    python scripts/a2a_ops.py grant-consumer --principal <object-id> [--scope agent|project]
                                              # let another identity call this agent over A2A
Reads usecase.yaml and FOUNDRY_PROJECT_ENDPOINT (from the environment or .env).
Incoming A2A: https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint
Outgoing A2A: peers become `a2a` tools in the use-case toolbox (see foundry_ops.py create-toolbox).
Only the f100-<id> agent and f100-<id>-a2a-* connections are created or changed.
"""
import argparse
import os
import subprocess
import sys

import httpx

A2A_VERSION = "1.0"
CONSUMER_ROLE_ID = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"  # Foundry Agent Consumer
CONNECTION_API_VERSION = "2025-04-01-preview"
FOUNDRY_HOST_SUFFIX = ".services.ai.azure.com"
PROBE_TEXT = "Reply with the single word: ready."
# Foundry can publish a card for a hosted agent before its A2A runtime accepts messages; that is
# reported, not failed, so the pipeline keeps working and starts passing once the service allows it.
UNSUPPORTED_MARKERS = ("HOSTED_AGENT_NOT_SUPPORTED", "HostedAgentNotSupported", "hosted-agent-not-supported")


class A2AError(RuntimeError):
    pass


def a2a_url(project_endpoint: str, agent: str) -> str:
    return f"{project_endpoint.rstrip('/')}/agents/{agent}/endpoint/protocols/a2a"


def _first_sentence(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    sentence = flat.split(". ")[0].rstrip(".")
    return (sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…")


def build_card(s: dict) -> dict:
    """Agent card derived from the use case: title, domain brief and one A2A skill per domain skill."""
    description = f"{s['title']}. {_first_sentence(s.get('domain_brief', ''), 240)}".strip()
    return {
        "description": description[:300],
        "version": A2A_VERSION,
        "skills": [
            {"id": skill["name"], "name": skill["name"].replace("-", " ").capitalize(),
             "description": skill["description"]}
            for skill in s["skills"]
        ],
    }


def enable_body(card: dict) -> dict:
    return {
        "agent_card": card,
        "agent_endpoint": {"protocol_configuration": {"responses": {}, "a2a": {}}},
    }


def enable(http: httpx.Client, endpoint: str, agent: str, card: dict) -> dict:
    """Publish the agent card and add the a2a protocol next to responses (merge patch: routing is kept)."""
    response = http.patch(f"{endpoint.rstrip('/')}/agents/{agent}", params={"api-version": "v1"},
                          json=enable_body(card))
    if response.status_code >= 300:
        raise A2AError(f"enable A2A on {agent}: HTTP {response.status_code} {response.text[:400]}")
    data = response.json()
    if "a2a" not in ((data.get("agent_endpoint") or {}).get("protocol_configuration") or {}):
        raise A2AError(f"enable A2A on {agent}: the service did not report the a2a protocol")
    return data


def fetch_card(http: httpx.Client, endpoint: str, agent: str) -> dict:
    url = f"{a2a_url(endpoint, agent)}/agentCard/v{A2A_VERSION}"
    response = http.get(url)
    if response.status_code >= 300:
        raise A2AError(f"agent card for {agent}: HTTP {response.status_code} {response.text[:300]}")
    card = response.json()
    versions = {item.get("protocolVersion") for item in card.get("supportedInterfaces") or []}
    if card.get("protocolVersion"):
        versions.add(card["protocolVersion"])
    if A2A_VERSION not in versions:
        raise A2AError(f"agent card for {agent} does not declare A2A {A2A_VERSION} (found {sorted(versions)})")
    return card


def probe(http: httpx.Client, endpoint: str, agent: str, text: str = PROBE_TEXT) -> tuple[str, str]:
    """Send one A2A v1.0 (JSON-RPC) message. Returns ("ok" | "unsupported" | "error", detail)."""
    body = {
        "jsonrpc": "2.0", "id": "f100-probe", "method": "SendMessage",
        "params": {"message": {"messageId": "f100-probe-1", "role": "ROLE_USER", "parts": [{"text": text}]}},
    }
    try:
        response = http.post(a2a_url(endpoint, agent), json=body, headers={"A2A-Version": A2A_VERSION})
    except httpx.HTTPError as exc:
        return "error", f"transport failure: {exc}"
    raw = response.text[:600]
    if any(marker in response.text for marker in UNSUPPORTED_MARKERS):
        return "unsupported", "the service does not accept A2A messages for this hosted agent yet"
    if response.status_code >= 300:
        return "error", f"HTTP {response.status_code} {raw}"
    payload = response.json()
    if payload.get("error"):
        return "error", str(payload["error"])[:400]
    return "ok", "message accepted"


def agent_protocols(http: httpx.Client, endpoint: str, agent: str) -> list[str] | None:
    """Protocols the latest agent version serves, or None when the agent does not exist."""
    response = http.get(f"{endpoint.rstrip('/')}/agents/{agent}", params={"api-version": "v1"})
    if response.status_code == 404:
        return None
    if response.status_code >= 300:
        raise A2AError(f"read agent {agent}: HTTP {response.status_code} {response.text[:300]}")
    definition = ((response.json().get("versions") or {}).get("latest") or {}).get("definition") or {}
    if definition.get("kind") == "prompt":
        return ["responses"]  # prompt agents serve the responses protocol by default
    return [item.get("protocol") for item in definition.get("protocol_versions") or []]


def verify(http: httpx.Client, endpoint: str, agent: str, expected_skills: list[str] | None = None) -> dict:
    card = fetch_card(http, endpoint, agent)
    published = {skill.get("id") for skill in card.get("skills") or []}
    missing = sorted(set(expected_skills or []) - published)
    if missing:
        raise A2AError(f"agent card for {agent} is missing skills {missing}")
    status, detail = probe(http, endpoint, agent)
    return {"agent": agent, "card_url": f"{a2a_url(endpoint, agent)}/agentCard/v{A2A_VERSION}",
            "skills": sorted(published), "message": status, "detail": detail}


def summary_line(result: dict) -> str:
    return (f"A2A {result['agent']}: card v{A2A_VERSION} published ({len(result['skills'])} skills); "
            f"message probe {result['message']} ({result['detail']})")


def peer_auth_type(target: str) -> str:
    host = httpx.URL(target).host or ""
    return "AgenticIdentityToken" if host.endswith(FOUNDRY_HOST_SUFFIX) else "None"


def connection_payload(target: str) -> dict:
    auth = peer_auth_type(target)
    properties = {
        "authType": auth, "category": "RemoteA2A", "target": target,
        "isSharedToAll": True, "Credentials": {}, "metadata": {},
    }
    if auth == "AgenticIdentityToken":
        properties["audience"] = "https://ai.azure.com"
    return {"properties": properties}


def _env_helpers():
    import foundry_ops  # sibling script; importing keeps env loading and project resolution in one place

    return foundry_ops


def _agent_http() -> httpx.Client:
    ops = _env_helpers()
    headers = {"Authorization": f"Bearer {ops.token()}", "Accept": "application/json"}
    return httpx.Client(headers=headers, timeout=httpx.Timeout(120.0))


def cmd_enable(check_only: bool) -> None:
    ops = _env_helpers()
    s, endpoint, agent = ops.spec(), ops.endpoint(), ops.names(ops.spec())["agent"]
    with _agent_http() as http:
        if not check_only:
            enable(http, endpoint, agent, build_card(s))
        result = verify(http, endpoint, agent, [skill["name"] for skill in s["skills"]])
    print(summary_line(result))
    print(f"  agent card: {result['card_url']}")
    print(f"  A2A endpoint: {a2a_url(endpoint, agent)} (select v{A2A_VERSION} with the A2A-Version header)")
    if result["message"] == "error":
        sys.exit(f"A2A message probe failed: {result['detail']}")


def _arm_put(url: str, payload: dict) -> None:
    token = subprocess.check_output(
        ["az", "account", "get-access-token", "--resource", "https://management.azure.com",
         "--query", "accessToken", "-o", "tsv"], text=True).strip()
    response = httpx.put(url, params={"api-version": CONNECTION_API_VERSION}, json=payload, timeout=120,
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    if response.status_code >= 300:
        sys.exit(f"connection PUT failed: HTTP {response.status_code} {response.text[:400]}")


def cmd_connect_peers() -> None:
    ops = _env_helpers()
    s, endpoint = ops.spec(), ops.endpoint()
    peers = ops.a2a_peers(s)
    if not peers:
        print("no peers with transport a2a in usecase.yaml; nothing to connect")
        return
    for peer in peers:
        name = ops.a2a_connection_name(s, peer)
        target = ops.peer_target(peer, endpoint)
        _arm_put(f"https://management.azure.com{ops.project_connection_id(name)}", connection_payload(target))
        print(f"A2A connection {name}: upserted -> {target} ({peer_auth_type(target)})")


def _scope(kind: str, agent: str | None) -> str:
    project_id = os.environ.get("AZURE_AI_PROJECT_ID")
    if not project_id:
        sys.exit("AZURE_AI_PROJECT_ID is not set (ARM id of the project)")
    return f"{project_id}/agents/{agent}" if kind == "agent" else project_id


def _assign(principal: str, scope: str) -> None:
    result = subprocess.run(
        ["az", "role", "assignment", "create", "--assignee-object-id", principal,
         "--assignee-principal-type", "ServicePrincipal", "--role", CONSUMER_ROLE_ID, "--scope", scope,
         "-o", "none"], capture_output=True, text=True)
    status = "ok" if result.returncode == 0 else f"failed: {result.stderr.strip()[:300]}"
    print(f"role Foundry Agent Consumer on {scope.split('/providers/')[-1]} for {principal}: {status}")
    if result.returncode != 0:
        sys.exit(1)


def cmd_grant_access() -> None:
    ops = _env_helpers()
    s = ops.spec()
    peers = [peer["agent"] for peer in ops.a2a_peers(s) if peer.get("agent")]
    if not peers:
        print("no Foundry agent peers with transport a2a in usecase.yaml; nothing to grant")
        return
    principal = ops.agent_principal(ops.names(s)["agent"])
    for peer in peers:
        _assign(principal, _scope("agent", peer))


def cmd_grant_consumer(principal: str, scope: str) -> None:
    ops = _env_helpers()
    _assign(principal, _scope(scope, ops.names(ops.spec())["agent"]))


def main() -> None:
    ops = _env_helpers()
    ops.load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("enable")
    sub.add_parser("verify")
    sub.add_parser("connect-peers")
    sub.add_parser("grant-access")
    consumer = sub.add_parser("grant-consumer")
    consumer.add_argument("--principal", required=True, help="object id of the calling identity")
    consumer.add_argument("--scope", choices=["agent", "project"], default="agent")
    args = parser.parse_args()
    try:
        if args.command == "enable":
            cmd_enable(check_only=False)
        elif args.command == "verify":
            cmd_enable(check_only=True)
        elif args.command == "connect-peers":
            cmd_connect_peers()
        elif args.command == "grant-access":
            cmd_grant_access()
        else:
            cmd_grant_consumer(args.principal, args.scope)
    except A2AError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
