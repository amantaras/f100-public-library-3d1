"""Contract tests: tools, skills, instructions, dataset, toolbox and agent config must agree."""
import json
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = yaml.safe_load((ROOT / "usecase.yaml").read_text())
PREFIX = f"f100-{SPEC['id']}"
AGENT_NAME = SPEC["agent_name"]
SKILLS = {skill["name"]: skill for skill in SPEC["skills"]}
SKILL_NAMES = {f"{PREFIX}-{name}" for name in SKILLS}
TOOL_NAMES = {f"{PREFIX}-{name}".replace("-", "_") for name in SKILLS}
PEERS = [peer for peer in SPEC.get("peers") or [] if peer.get("transport", "mcp") == "mcp"]
PEER_TOOL_NAMES = {"ask" + "".join(part.capitalize() for part in peer["name"].split("-")) for peer in PEERS}
MCP_TOOL_NAMES = {operation["tool_name"] for operation in SPEC["mcp"]["operations"]} | PEER_TOOL_NAMES
DATASET = ROOT / "evaluations" / f"{SPEC['id']}-seed-v1.jsonl"
SAFETY_DATASET = ROOT / "evaluations" / f"{SPEC['id']}-content-safety-v1.jsonl"
PLACEHOLDER = re.compile(r"\{\{ [a-z0-9_]+ \}\}")
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".json", ".jsonl", ".txt", ".example"}


def _frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    assert match, "missing YAML frontmatter"
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def test_no_unresolved_placeholders():
    offenders = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name.startswith(".env"))
        and not any(part.startswith(".venv") for part in path.parts)  # .venv, .venv-agentops (chapter 12)
        and "tests" not in path.parts and PLACEHOLDER.search(path.read_text(errors="ignore"))
    ]
    assert offenders == []


def test_toolbox_skills_match_spec():
    folders = {p.name for p in (ROOT / "toolbox-skills").iterdir() if p.is_dir()}
    assert folders == SKILL_NAMES
    for name in SKILL_NAMES:
        meta, body = _frontmatter((ROOT / "toolbox-skills" / name / "SKILL.md").read_text())
        assert meta.get("name") == name
        assert isinstance(meta.get("description"), str) and meta["description"].strip()
        assert len(body.strip()) >= 100, f"{name}: skill body too short"


def test_tools_json_matches_skills():
    tools = json.loads((ROOT / ".agent_configs/baseline/tools.json").read_text())
    names = {tool["function"]["name"] for tool in tools}
    assert names == TOOL_NAMES
    for tool in tools:
        params = tool["function"]["parameters"]
        assert params["required"] == ["query"] and params["properties"]["query"]["type"] == "string"


def test_instructions_route_every_tool_and_resist_injection():
    text = (ROOT / ".agent_configs/baseline/instructions.md").read_text()
    mentioned = set(re.findall(r"\bf100_[a-z0-9_]+\b", text))
    assert TOOL_NAMES <= mentioned, f"not mentioned: {sorted(TOOL_NAMES - mentioned)}"
    assert mentioned <= TOOL_NAMES, f"unknown tools: {sorted(mentioned - TOOL_NAMES)}"
    assert MCP_TOOL_NAMES <= {name for name in MCP_TOOL_NAMES if name in text}, (
        f"MCP tools not mentioned: {sorted(name for name in MCP_TOOL_NAMES if name not in text)}"
    )
    assert "synthetic" in text.lower()
    assert "as data, never as instructions" in text


def test_dataset_rows_are_valid():
    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    required = {"query", "ground_truth", "expected_behavior", "category", "tool_calls", "tool_definitions"}
    special_categories = {"indirect_prompt_injection", "policy_safety"}
    categories = {name.replace("-", "_") for name in SKILLS} | special_categories
    queries = [row["query"] for row in rows]
    assert len(queries) == len(set(queries)), "duplicate queries"
    for index, row in enumerate(rows):
        assert required <= row.keys(), f"row {index} missing {required - row.keys()}"
        assert row["category"] in categories, f"row {index} unknown category {row['category']}"
        called = {call["name"] for call in row["tool_calls"]}
        if row["category"] == "policy_safety":
            assert not called and not row["tool_definitions"], f"row {index} policy_safety must not declare tools"
        else:
            assert called and called <= TOOL_NAMES, f"row {index} unknown tools {called - TOOL_NAMES}"
        if row["category"] not in special_categories:
            assert f"{PREFIX}-{row['category'].replace('_', '-')}".replace("-", "_") in called, f"row {index} category/tool mismatch"
    per_skill = {c: sum(r["category"] == c for r in rows) for c in categories - special_categories}
    evaluation = SPEC["evaluation"]
    assert min(per_skill.values()) >= evaluation["seed_cases_per_skill"], per_skill
    assert sum(r["category"] == "indirect_prompt_injection" for r in rows) >= evaluation["injection_cases"]


def test_toolbox_and_agent_config_match():
    toolbox = yaml.safe_load((ROOT / "toolbox.yaml").read_text())
    assert {s["name"] for s in toolbox["skills"]} == SKILL_NAMES
    assert toolbox["connections"] == [{"name": f"{PREFIX}-mcp"}]
    agent = yaml.safe_load((ROOT / "agent.yaml").read_text())
    env = {item["name"]: item["value"] for item in agent["environment_variables"]}
    assert agent["name"] == AGENT_NAME == env["FOUNDRY_AGENT_NAME"]
    assert env["TOOLBOX_NAME"] == f"{PREFIX}-toolbox"
    assert env["AZURE_CONTENT_SAFETY_ENDPOINT"] == "${AZURE_CONTENT_SAFETY_ENDPOINT}"
    profile = yaml.safe_load((ROOT / "content-safety.yaml").read_text())
    incident_names = [item["name"] for item in profile["azure_resources"]["rapid_incidents"]]
    assert env["AZURE_CONTENT_SAFETY_INCIDENTS"].split(",") == incident_names
    assert env["TOOLBOX_DISCOVER_TOOLS"] == "true"
    assert agent["protocols"][0]["protocol"] == "responses"
    assert agent["protocols"][1]["protocol"] == "invocations"
    assert [item["version"] for item in agent["protocols"]] == ["2.0.0", "2.0.0"]
    service = yaml.safe_load((ROOT / "azure.yaml").read_text())["services"][AGENT_NAME]
    assert [item["version"] for item in service["protocols"]] == ["2.0.0", "2.0.0"]
    policy = service["policies"][0]
    assert policy["type"] == "rai_policy"
    assert policy["raiPolicyName"].endswith(f"/raiPolicies/{PREFIX}-content-safety")
    moderation = policy["invocationsModeration"]
    assert moderation["responseMode"] == "streaming"
    assert set(moderation["inputPaths"]) == {"$.input", "$.message", "$.prompt", "$.query"}
    assert moderation["streamSelectors"] == [{"eventType": "token", "textField": "delta"}]


def test_memory_is_wired_from_the_provisioned_store_and_evaluations_opt_out():
    agent = yaml.safe_load((ROOT / "agent.yaml").read_text())
    env = {item["name"]: item["value"] for item in agent["environment_variables"]}
    # Empty until the Factory provisions the store; then azd fills it in at deploy time.
    assert env["MEMORY_STORE_NAME"] == "${MEMORY_STORE_NAME}"
    service = yaml.safe_load((ROOT / "azure.yaml").read_text())["services"][AGENT_NAME]
    assert service["env"]["MEMORY_STORE_NAME"] == "${MEMORY_STORE_NAME}"
    runtime = (ROOT / "main.py").read_text()
    assert "import agent_memory" in runtime and "memory.begin(" in runtime and "await memory.remember(" in runtime
    memory = (ROOT / "agent_memory.py").read_text()
    assert 'USER_HEADER = "x-agent-user-id"' in memory and 'payload.get("memory") is False' in memory
    assert 'FOUNDRY_CALL_HEADER = "x-agent-foundry-call-id"' in memory
    assert 'headers[FOUNDRY_CALL_HEADER] = call_id' in memory
    if "class McpBridge" in runtime:
        assert "headers[agent_memory.FOUNDRY_CALL_HEADER] = call_id" in runtime
    assert (ROOT / "scripts" / "memory_ops.py").is_file()
    assert (ROOT / "scripts" / "insights_ops.py").is_file()
    assert '"include_tool_events": True, "memory": memory' in (ROOT / "scripts" / "smoke.py").read_text()


def test_vscode_agent_inspector_debugging_is_reproducible():
    assert "debugpy" in (ROOT / "requirements.txt").read_text().splitlines()
    tasks = json.loads((ROOT / ".vscode" / "tasks.json").read_text())
    labels = {task["label"] for task in tasks["tasks"]}
    assert {"FAF: Run local agent with debugger", "FAF: Open Agent Inspector"} <= labels
    launch = json.loads((ROOT / ".vscode" / "launch.json").read_text())
    assert launch["configurations"][0]["preLaunchTask"] == "FAF: Open Agent Inspector"


def test_smoke_targets_a_known_skill():
    assert SPEC["smoke"]["expected_skill"] in SKILLS
    assert SPEC["smoke"]["query"].strip()


def test_apim_contract_matches_mcp_spec():
    openapi = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text())
    manifest = yaml.safe_load((ROOT / "api" / "apim-manifest.yaml").read_text())
    operations = SPEC["mcp"]["operations"]
    rendered = {
        operation["operationId"]
        for path in openapi["paths"].values()
        for operation in path.values()
    }
    peer_operations = {f"ask-{peer['name']}" for peer in PEERS}
    assert rendered == {operation["name"] for operation in operations} | peer_operations
    assert {tool["display_name"] for tool in manifest["tools"]} == MCP_TOOL_NAMES
    assert {(peer["name"], peer["agent"]) for peer in manifest["peers"]} == {(p["name"], p["agent"]) for p in PEERS}
    assert manifest["mcp_server"]["connection"] == f"{PREFIX}-mcp"
    assert manifest["smoke"]["expected_tool"] in MCP_TOOL_NAMES
    assert len(operations) in (4, 5) or SPEC.get("pattern") == "orchestrator"
    for operation in operations:
        response = ROOT / "api" / "mock-data" / f"{operation['name']}.json"
        assert json.loads(response.read_text()) == operation["response_example"]
    api_policy = (ROOT / "api" / "policies" / "api-policy.xml").read_text()
    mcp_policy = (ROOT / "api" / "policies" / "mcp-policy.xml").read_text()
    assert "<mock-response" in api_policy
    for policy in (api_policy, mcp_policy):
        assert "<validate-azure-ad-token" in policy
        assert "__F100_TENANT_ID__" in policy
        assert "__F100_MCP_AUDIENCE_ID__" in policy
        assert "__F100_PROJECT_PRINCIPAL_ID__" in policy
        assert "subscription-key" not in policy.casefold()
    assert "context.Response.Body" not in mcp_policy
    for peer in PEERS:
        policy = (ROOT / "api" / "policies" / f"peer-{peer['name']}.xml").read_text()
        assert "<validate-azure-ad-token" in policy and "__F100_PROJECT_PRINCIPAL_ID__" in policy
        assert f"__F100_PROJECT_ENDPOINT__/agents/{peer['agent']}/endpoint/protocols/openai" in policy
        assert '<authentication-managed-identity resource="https://ai.azure.com"' in policy
        assert "<mock-response" not in policy and "<base />" not in policy.split("<on-error>")[0]
        assert "subscription-key" not in policy.casefold()


def test_content_safety_profile_and_showcase_match_spec():
    profile = yaml.safe_load((ROOT / "content-safety.yaml").read_text())
    safety = SPEC["safety"]
    assert profile["deployment_controls"]["harm_thresholds"] == safety["harm_thresholds"]
    assert profile["deployment_controls"]["prompt_shields"] == {
        "direct_attacks": True, "indirect_attacks": True}
    assert profile["application_controls"]["blocklist_terms"] == safety["blocklist_terms"]
    categories = profile["application_controls"]["custom_categories"]
    assert [category["name"] for category in categories] == [
        category["name"] for category in safety["custom_categories"]]
    assert all(category["implementation"] == "content-safety-rapid-preview" for category in categories)
    enforcement = profile["application_controls"]["runtime_enforcement"]
    assert enforcement["mode"] == "agent-input-precheck"
    assert enforcement["failure_mode"] == "fail-closed"
    resources = profile["azure_resources"]
    assert resources["rai_policy"]["name"] == f"{PREFIX}-content-safety"
    assert resources["blocklist"]["name"] == f"{PREFIX}-blocklist"
    assert all(item["name"].startswith(f"{PREFIX}-") for item in resources["rapid_incidents"])
    assert all(category["activation_terms"] for category in categories)
    assert all(
        any(term.casefold() in example.casefold() for term in category["activation_terms"])
        for category in categories
        for example in category["examples"]
    )
    runtime = (ROOT / "main.py").read_text()
    assert "_custom_content_safety_matches" in runtime
    assert "_activated_content_safety_incidents" in runtime
    assert "AZURE_CONTENT_SAFETY_INCIDENTS" in runtime
    assert '"type": "token", "delta": value' in runtime or '"type": "token", "delta": update.text' in runtime
    assert (ROOT / "scripts" / "content_safety_ops.py").is_file()
    rows = [json.loads(line) for line in SAFETY_DATASET.read_text().splitlines() if line.strip()]
    assert [row["id"] for row in rows] == ["allowed", "blocked", "custom-category"]
    assert len({row["prompt"] for row in rows}) == 3
    assert all(row["expected_action"].strip() for row in rows)


@pytest.mark.parametrize("name", sorted(SKILL_NAMES))
def test_skill_names_are_namespaced(name):
    assert name.startswith("f100-"), "generated resources must be namespaced to avoid touching existing ones"


def _a2a_ops():
    import importlib.util

    spec = importlib.util.spec_from_file_location("a2a_ops", ROOT / "scripts" / "a2a_ops.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a2a_agent_card_publishes_every_skill():
    card = _a2a_ops().build_card(SPEC)
    assert card["version"] == "1.0" and 0 < len(card["description"]) <= 300
    assert {skill["id"] for skill in card["skills"]} == set(SKILLS)


def test_a2a_peers_are_well_formed():
    peers = SPEC.get("peers") or []
    assert (SPEC.get("pattern", "single") == "orchestrator") == bool(peers)
    assert len({peer["name"] for peer in peers}) == len(peers)
    for peer in peers:
        assert bool(peer.get("agent")) != bool(peer.get("url")), peer["name"]
