"""Per-user Foundry memory for this agent: provision the memory store and prove the deployed agent remembers.

    python scripts/memory_ops.py provision --embedding-model text-embedding-3-small   # get-or-create the store
    python scripts/memory_ops.py verify-agent --agent <agent-name>                    # remember in one session, recall in another

The store is namespaced f100-<id>-memory and tagged workshop=foundry100. It enables user profiles, chat summaries and
procedural memory. User profile extraction excludes sensitive data and memories expire after 90 days. An existing
store is never modified. The agent reads the store name from MEMORY_STORE_NAME (see
agent_memory.py) and scopes memories to the platform user (x-agent-user-id), so users never see each other's memories.

verify-agent tells the deployed agent a synthetic preferred name in one session, waits for the service to extract it,
asks for it in a new session, and then deletes the memories it created (the caller's scope). Pass ``--retain`` only when
you explicitly want temporary portal evidence; delete that synthetic scope after inspection. Reads usecase.yaml and
FOUNDRY_PROJECT_ENDPOINT (from the environment, .env or the selected azd environment). Writes .foundry/memory.json.
"""
import argparse
import json
import os
import pathlib
import random
import re
import string
import subprocess
import sys
import time
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / ".foundry" / "memory.json"
TTL_SECONDS = 90 * 24 * 3600


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


def store_name(data: dict | None = None, name: str | None = None) -> str:
    default = f"f100-{(data or spec())['id']}-memory"
    state = read_state()
    selected = state.get("name") if state.get("status") in ("provisioned", "verified") else None
    name = name or selected or default
    if not re.fullmatch(rf"{re.escape(default)}(?:-[a-z0-9]+)*", name):
        raise ValueError(f"memory store name must be {default} or use that prefix with a lowercase alphanumeric suffix")
    return name


def profile_details(data: dict) -> str:
    return (f"Remember stable preferences the user states about how they want help with {data['title']}: preferred name, "
            "language, answer format and recurring, non-sensitive context about their work. Never store credentials, "
            "account, card or government ID numbers, health, financial or legal details, precise location, or anything the "
            "user asks you to forget.")


def endpoint() -> str:
    value = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not value.startswith("https://"):
        sys.exit("FOUNDRY_PROJECT_ENDPOINT is not set (see .env.example)")
    return value


def project_client(project_endpoint: str):
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    return AIProjectClient(endpoint=project_endpoint, credential=AzureCliCredential(), allow_preview=True,
                           connection_timeout=30, read_timeout=120)


def read_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.is_file() else {}


def write_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def provision(project_endpoint: str, chat_model: str, embedding_model: str, name: str | None = None) -> dict:
    from azure.ai.projects.models import MemoryStoreDefaultDefinition, MemoryStoreDefaultOptions
    from azure.core.exceptions import ResourceNotFoundError

    data = spec()
    name = store_name(data, name)
    with project_client(project_endpoint) as project:
        try:
            existing = project.beta.memory_stores.get(name)
            action = "kept"
            definition = existing.get("definition") or {}
        except ResourceNotFoundError:
            options = MemoryStoreDefaultOptions(
                user_profile_enabled=True,
                user_profile_details=profile_details(data),
                chat_summary_enabled=True,
                procedural_memory_enabled=True,
                default_ttl_seconds=TTL_SECONDS,
            )
            created = project.beta.memory_stores.create(
                name=name,
                description=f"Per-user memory for {data.get('agent_name') or data['id']} (Foundry Agent Factory)",
                definition=MemoryStoreDefaultDefinition(chat_model=chat_model, embedding_model=embedding_model,
                                                        options=options),
                metadata={"workshop": "foundry100", "generator": "foundry-agent-factory", "agent": data.get("agent_name") or data["id"]},
            )
            action = "created"
            definition = created.get("definition") or {}
    state = {
        "schema_version": 1,
        "status": "provisioned",
        "name": name,
        "action": action,
        "chat_model": definition.get("chat_model", chat_model),
        "embedding_model": definition.get("embedding_model", embedding_model),
        "provisioned_at": datetime.now(timezone.utc).isoformat(),
        "verified": None,
    }
    write_state(state)
    return state


def _codename() -> str:
    return "Quill-" + "".join(random.choices(string.ascii_uppercase, k=4))


def _memories_for(project, name: str, scope: str, query: str) -> list[str]:
    result = project.beta.memory_stores.search_memories(
        name, scope=scope, items=[{"type": "message", "role": "user", "content": query}])
    return [item.memory_item.content for item in result.memories or [] if item.memory_item and item.memory_item.content]


def verify_agent(project_endpoint: str, agent: str, version: str | None, attempts: int, interval: int,
                 retain: bool = False) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    from smoke import invoke_deployed, latest_version, token

    name = store_name()
    version = version or latest_version(project_endpoint, agent, token())
    codename = _codename()
    told = invoke_deployed(project_endpoint, agent, version,
                           f"Please remember for our future conversations that I like to be called {codename}.",
                           memory=True)
    event = told.get("memory") or {}
    if told["status"] != 200 or told["errors"] or not told["done"]:
        raise RuntimeError(f"the remember turn did not complete: HTTP {told['status']} {str(told['errors'])[:300]}")
    if not event:
        raise RuntimeError("the agent reported no memory event: its runtime predates agent_memory.py (redeploy it)")
    if not event.get("active"):
        raise RuntimeError(f"memory is not active in the deployed agent: {event.get('reason') or 'unknown reason'}")
    if not event.get("stored"):
        raise RuntimeError(f"the agent could not store the turn: {event.get('reason') or 'unknown reason'}")
    scope = event.get("scope")
    question = "Quick check before we start: what name did I ask you to call me?"
    started = time.monotonic()
    recalled: dict = {}
    remembered: list[str] = []
    try:
        with project_client(project_endpoint) as project:
            for attempt in range(1, attempts + 1):
                time.sleep(interval)
                remembered = [m for m in _memories_for(project, name, scope, question) if codename.lower() in m.lower()]
                if not remembered:
                    print(f"attempt {attempt}/{attempts}: the store has not extracted the fact yet", flush=True)
                    continue
                recalled = invoke_deployed(project_endpoint, agent, version, question, memory=True)
                count = (recalled.get("memory") or {}).get("recalled", 0)
                print(f"attempt {attempt}/{attempts}: store holds the fact; the agent recalled {count} memories; "
                      f"answer names it: {codename.lower() in recalled['text'].lower()}", flush=True)
                if count:
                    break
            else:
                raise RuntimeError(
                    "the deployed agent did not recall the remembered fact in a new session "
                    + ("(the store never extracted it)" if not remembered else "(the store holds it, recall returned nothing)")
                    + f" within {attempts * interval}s")
    finally:
        if scope and not retain:
            with project_client(project_endpoint) as project:
                project.beta.memory_stores.delete_scope(name, scope=scope)
            print("deleted the memories this verification created", flush=True)
        elif scope:
            print("retained the synthetic verification memory; delete its scope after portal inspection", flush=True)
    state = read_state() or {"schema_version": 1, "name": name}
    state["verified"] = {
        "agent": agent, "version": str(version), "verified_at": datetime.now(timezone.utc).isoformat(),
        "seconds_to_recall": round(time.monotonic() - started), "answer_names_fact": codename.lower() in recalled["text"].lower(),
        "retained": retain,
    }
    state["status"] = "verified"
    write_state(state)
    return state


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prov = sub.add_parser("provision")
    prov.add_argument("--embedding-model", required=True, help="embedding model deployment in the project")
    prov.add_argument("--chat-model", help="chat model deployment (default: the agent's model)")
    prov.add_argument("--name", help="explicit replacement store name within this agent's f100- namespace")
    check = sub.add_parser("verify-agent")
    check.add_argument("--agent", default=None, help="deployed agent name (default: the use case's agent)")
    check.add_argument("--version", help="agent version (default: latest)")
    check.add_argument("--attempts", type=int, default=16)
    check.add_argument("--interval", type=int, default=30, help="seconds between recall attempts")
    check.add_argument("--retain", action="store_true",
                       help="retain the synthetic memory for portal inspection instead of deleting its scope")
    sub.add_parser("name", help="print the memory store name")
    args = parser.parse_args()
    if args.command == "name":
        print(store_name())
        return
    project_endpoint = endpoint()
    if args.command == "provision":
        data = spec()
        chat = args.chat_model or os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or data["model_deployment"]
        state = provision(project_endpoint, chat, args.embedding_model, args.name)
        print(f"MEMORY_STORE_PROVISIONED name={state['name']} ({state['action']}) chat_model={state['chat_model']} "
              f"embedding_model={state['embedding_model']}")
    else:
        agent = args.agent or spec().get("agent_name")
        if not agent:
            sys.exit("usecase.yaml has no agent_name; pass --agent <deployed agent name>")
        state = verify_agent(project_endpoint, agent, args.version, args.attempts, args.interval, args.retain)
        verified = state["verified"]
        print(f"MEMORY_VERIFIED agent={agent} version={verified['version']} store={state['name']} "
              f"recall_after={verified['seconds_to_recall']}s answer_names_fact={verified['answer_names_fact']}")


if __name__ == "__main__":
    main()
