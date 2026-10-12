"""Provision and verify the per-use-case Content Safety resources."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import yaml
from azure.identity import AzureCliCredential

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARM_ENDPOINT = "https://management.azure.com"
ARM_API_VERSION = "2025-09-01"
CONTENT_SAFETY_API_VERSION = "2024-02-15-preview"
TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 5


def load_env() -> None:
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    if (ROOT / ".azure").is_dir():
        result = subprocess.run(
            ["azd", "env", "get-values"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                key, _, value = line.partition("=")
                if key and value:
                    os.environ.setdefault(key.strip(), value.strip().strip('"'))


def spec() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "usecase.yaml").read_text()) or {}


def profile() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "content-safety.yaml").read_text()) or {}


def account_id(project_id: str) -> str:
    marker = "/projects/"
    if marker not in project_id:
        raise ValueError("AZURE_AI_PROJECT_ID must be a Foundry project ARM resource ID")
    value = project_id.split(marker, 1)[0].rstrip("/")
    if "/providers/Microsoft.CognitiveServices/accounts/" not in value:
        raise ValueError("AZURE_AI_PROJECT_ID does not identify a Microsoft.CognitiveServices account")
    return value


def project_id(value: str | None = None) -> str:
    resolved = (value or os.environ.get("AZURE_AI_PROJECT_ID") or "").strip()
    if not resolved:
        raise ValueError("AZURE_AI_PROJECT_ID is not set")
    account_id(resolved)
    return resolved


def project_endpoint(value: str | None = None) -> str:
    resolved = (
        value
        or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or ""
    ).rstrip("/")
    if not resolved.startswith("https://") or "/api/projects/" not in resolved:
        raise ValueError("FOUNDRY_PROJECT_ENDPOINT is not a Foundry project endpoint")
    return resolved


def content_safety_endpoint() -> str:
    resolved = (os.environ.get("AZURE_CONTENT_SAFETY_ENDPOINT") or "").rstrip("/")
    if not resolved.startswith("https://"):
        raise ValueError("AZURE_CONTENT_SAFETY_ENDPOINT is not set")
    return resolved


def resource_names(data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or profile()
    resources = data.get("azure_resources") or {}
    policy = resources.get("rai_policy") or {}
    blocklist = resources.get("blocklist") or {}
    incidents = resources.get("rapid_incidents") or []
    if not isinstance(policy.get("name"), str) or not isinstance(blocklist.get("name"), str):
        raise ValueError("content-safety.yaml is missing Azure RAI policy or blocklist names")
    if not incidents or any(not isinstance(item.get("name"), str) for item in incidents):
        raise ValueError("content-safety.yaml is missing rapid incident names")
    prefix = f"f100-{spec()['id']}"
    names = [policy["name"], blocklist["name"], *(item["name"] for item in incidents)]
    if any(not name.startswith(prefix) for name in names):
        raise ValueError("Content Safety resources must use the generated use-case prefix")
    return {"policy": policy["name"], "blocklist": blocklist["name"], "incidents": incidents}


def activation_rules(data: dict[str, Any] | None = None) -> dict[str, list[str]]:
    data = data or profile()
    names = resource_names(data)
    by_source = {item["source_name"]: item["name"] for item in names["incidents"]}
    rules: dict[str, list[str]] = {}
    for category in data["application_controls"]["custom_categories"]:
        incident = by_source.get(category["name"])
        terms = category.get("activation_terms")
        if not incident or not isinstance(terms, list) or not terms or any(not isinstance(term, str) for term in terms):
            raise ValueError(f"custom category {category.get('name')} has no valid activation terms")
        rules[incident] = terms
    return rules


def activated_incidents(text: str, data: dict[str, Any] | None = None) -> list[str]:
    rules = activation_rules(data)
    folded = text.casefold()
    return [
        incident
        for incident, terms in rules.items()
        if any(term.casefold() in folded for term in terms)
    ]


def rai_policy_id(target_project_id: str | None = None) -> str:
    return f"{account_id(project_id(target_project_id))}/raiPolicies/{resource_names()['policy']}"


def configure_manifest(target_project_id: str | None = None) -> str:
    expected = rai_policy_id(target_project_id)
    path = ROOT / "azure.yaml"
    text = path.read_text()
    pattern = re.compile(r"^(?P<indent>\s*)raiPolicyName:\s*\S+\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError("azure.yaml must contain exactly one raiPolicyName")
    current = matches[0].group(0).split(":", 1)[1].strip()
    if current != expected:
        text = pattern.sub(
            lambda match: f"{match.group('indent')}raiPolicyName: {expected}",
            text,
            count=1,
        )
        path.write_text(text)
    return expected


def _credential() -> AzureCliCredential:
    return AzureCliCredential()


def _token(scope: str) -> str:
    return _credential().get_token(scope).token


def _client(base_url: str, scope: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {_token(scope)}", "Accept": "application/json"},
        timeout=90,
    )


def _send(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    expected: set[int],
    label: str,
    **kwargs: Any,
) -> httpx.Response:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == RETRY_ATTEMPTS:
                raise RuntimeError(f"{label}: {exc}") from exc
            reason = type(exc).__name__
        else:
            if response.status_code in expected:
                return response
            if response.status_code not in TRANSIENT_STATUS or attempt == RETRY_ATTEMPTS:
                raise RuntimeError(f"{label}: HTTP {response.status_code} {response.text[:600]}")
            reason = f"HTTP {response.status_code}"
        delay = min(2 ** attempt, 20)
        print(f"{label}: transient {reason}; retrying in {delay}s", file=sys.stderr)
        time.sleep(delay)
    raise AssertionError("unreachable")


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Azure returned invalid JSON for {response.request.method} {response.request.url}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Azure returned a non-object response for {response.request.method} {response.request.url}")
    return value


def _resource_tags() -> dict[str, str]:
    return {"workshop": "foundry100", "usecase": spec()["id"]}


def _arm_path(target_project_id: str | None, suffix: str) -> str:
    return f"{account_id(project_id(target_project_id))}/{suffix}?api-version={ARM_API_VERSION}"


def blocklist_item_name(index: int, term: str) -> str:
    digest = hashlib.sha256(term.encode()).hexdigest()[:10]
    return f"term-{index:02d}-{digest}"


def _blocklist_items(
    client: httpx.Client,
    target_project_id: str | None,
    blocklist: str,
) -> dict[str, dict[str, Any]]:
    url = _arm_path(target_project_id, f"raiBlocklists/{blocklist}/raiBlocklistItems")
    items: dict[str, dict[str, Any]] = {}
    while url:
        payload = _json(
            _send(
                client,
                "GET",
                url,
                expected={200},
                label=f"list blocklist items for {blocklist}",
            )
        )
        for item in payload.get("value") or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise RuntimeError(f"blocklist {blocklist} returned an invalid item")
            items[item["name"]] = item
        next_link = payload.get("nextLink")
        url = next_link if isinstance(next_link, str) and next_link else ""
    return items


def content_filters(data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = data or profile()
    controls = data["deployment_controls"]
    harm_names = {
        "hate": "Hate",
        "sexual": "Sexual",
        "violence": "Violence",
        "self_harm": "Selfharm",
    }
    filters = []
    for key, name in harm_names.items():
        threshold = str(controls["harm_thresholds"][key]).title()
        for source in ("Prompt", "Completion"):
            filters.append(
                {
                    "name": name,
                    "enabled": True,
                    "blocking": True,
                    "severityThreshold": threshold,
                    "source": source,
                }
            )
    if controls["prompt_shields"]["direct_attacks"]:
        filters.append({"name": "Jailbreak", "enabled": True, "blocking": True, "source": "Prompt"})
    if controls["prompt_shields"]["indirect_attacks"]:
        filters.append({"name": "Indirect Attack", "enabled": True, "blocking": True, "source": "Prompt"})
    if controls["protected_material"]["text"]:
        filters.append(
            {"name": "Protected Material Text", "enabled": True, "blocking": True, "source": "Completion"}
        )
    if controls["protected_material"]["code"]:
        filters.append(
            {"name": "Protected Material Code", "enabled": True, "blocking": True, "source": "Completion"}
        )
    return filters


def rai_policy_payload(data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or profile()
    blocklist = resource_names(data)["blocklist"]
    return {
        "tags": _resource_tags(),
        "properties": {
            "basePolicyName": "Microsoft.DefaultV2",
            "mode": "Blocking",
            "contentFilters": content_filters(data),
            "customBlocklists": [
                {"source": source, "blocklistName": blocklist, "blocking": True}
                for source in ("Prompt", "Completion")
            ],
        },
    }


def _normalize_filter(value: dict[str, Any]) -> tuple:
    return (
        value.get("name"),
        value.get("source"),
        value.get("enabled"),
        value.get("blocking"),
        value.get("severityThreshold"),
    )


def _normalize_blocklist(value: dict[str, Any]) -> tuple:
    return value.get("blocklistName"), value.get("source"), value.get("blocking")


def provision_arm(target_project_id: str | None = None) -> dict[str, Any]:
    data = profile()
    names = resource_names(data)
    terms = data["application_controls"]["blocklist_terms"]
    with _client(ARM_ENDPOINT, "https://management.azure.com/.default") as client:
        _send(
            client,
            "PUT",
            _arm_path(target_project_id, f"raiBlocklists/{names['blocklist']}"),
            expected={200, 201},
            label=f"create blocklist {names['blocklist']}",
            json={
                "tags": _resource_tags(),
                "properties": {"description": f"Foundry Agent Factory exact-match terms for {spec()['title']}"},
            },
        )
        expected_items = {
            blocklist_item_name(index, term)
            for index, term in enumerate(terms, 1)
        }
        for stale in sorted(set(_blocklist_items(client, target_project_id, names["blocklist"])) - expected_items):
            _send(
                client,
                "DELETE",
                _arm_path(
                    target_project_id,
                    f"raiBlocklists/{names['blocklist']}/raiBlocklistItems/{stale}",
                ),
                expected={200, 202, 204, 404},
                label=f"delete stale blocklist item {stale}",
            )
        for index, term in enumerate(terms, 1):
            item = blocklist_item_name(index, term)
            _send(
                client,
                "PUT",
                _arm_path(
                    target_project_id,
                    f"raiBlocklists/{names['blocklist']}/raiBlocklistItems/{item}",
                ),
                expected={200, 201},
                label=f"create blocklist item {item}",
                json={
                    "tags": _resource_tags(),
                    "properties": {"pattern": term, "isRegex": False},
                },
            )
        policy = _send(
            client,
            "PUT",
            _arm_path(target_project_id, f"raiPolicies/{names['policy']}"),
            expected={200, 201},
            label=f"create RAI policy {names['policy']}",
            json=rai_policy_payload(data),
        )
    return _json(policy)


def verify_arm(target_project_id: str | None = None) -> dict[str, Any]:
    data = profile()
    names = resource_names(data)
    terms = data["application_controls"]["blocklist_terms"]
    with _client(ARM_ENDPOINT, "https://management.azure.com/.default") as client:
        policy = _json(
            _send(
                client,
                "GET",
                _arm_path(target_project_id, f"raiPolicies/{names['policy']}"),
                expected={200},
                label=f"read RAI policy {names['policy']}",
            )
        )
        properties = policy.get("properties") or {}
        expected_filters = {_normalize_filter(value) for value in content_filters(data)}
        actual_filters = {_normalize_filter(value) for value in properties.get("contentFilters") or []}
        if not expected_filters <= actual_filters:
            raise RuntimeError(f"RAI policy is missing filters: {sorted(expected_filters - actual_filters)}")
        expected_lists = {
            (names["blocklist"], source, True) for source in ("Prompt", "Completion")
        }
        actual_lists = {
            _normalize_blocklist(value) for value in properties.get("customBlocklists") or []
        }
        if not expected_lists <= actual_lists:
            raise RuntimeError(f"RAI policy is missing blocklist bindings: {sorted(expected_lists - actual_lists)}")
        actual_items = _blocklist_items(client, target_project_id, names["blocklist"])
        expected_items = {
            blocklist_item_name(index, term): term
            for index, term in enumerate(terms, 1)
        }
        if set(actual_items) != set(expected_items):
            raise RuntimeError(
                f"blocklist items differ from content-safety.yaml: "
                f"expected {sorted(expected_items)}, found {sorted(actual_items)}"
            )
        for index, term in enumerate(terms, 1):
            item = blocklist_item_name(index, term)
            actual = actual_items[item].get("properties") or {}
            if actual.get("pattern") != term or actual.get("isRegex") is not False:
                raise RuntimeError(f"blocklist item {item} does not match content-safety.yaml")
    return policy


def _sample_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    direct = value.get("text")
    if isinstance(direct, str):
        return direct
    sample = value.get("incidentSample") or value.get("sample")
    return sample.get("text") if isinstance(sample, dict) and isinstance(sample.get("text"), str) else None


def _sample_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("incidentSampleId", "sampleId", "id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate.rsplit("/", 1)[-1]
    return None


def _sample_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("value", "incidentSamples", "IncidentSamples", "samples"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _existing_sample_texts(client: httpx.Client, incident: str) -> set[str]:
    base = f"/contentsafety/text/incidents/{incident}/incidentsamples"
    response = _send(
        client,
        "GET",
        base,
        expected={200, 404},
        label=f"list rapid incident samples {incident}",
        params={"api-version": CONTENT_SAFETY_API_VERSION},
    )
    if response.status_code == 404:
        return set()
    rows = _sample_rows(_json(response))
    texts = {text for row in rows if (text := _sample_text(row))}
    unresolved = [sample_id for row in rows if not _sample_text(row) and (sample_id := _sample_id(row))]
    for sample_id in unresolved:
        detail = _json(
            _send(
                client,
                "GET",
                f"{base}/{sample_id}",
                expected={200},
                label=f"read rapid incident sample {sample_id}",
                params={"api-version": CONTENT_SAFETY_API_VERSION},
            )
        )
        text = _sample_text(detail)
        if not text:
            raise RuntimeError(f"rapid incident sample {sample_id} has no text")
        texts.add(text)
    if rows and len(texts) != len(rows):
        raise RuntimeError(f"could not inspect every existing sample for rapid incident {incident}")
    return texts


def detect_incidents(client: httpx.Client, text: str, incidents: list[str]) -> list[str]:
    response = _send(
        client,
        "POST",
        "/contentsafety/text:detectIncidents",
        expected={200},
        label="detect rapid incidents",
        params={"api-version": CONTENT_SAFETY_API_VERSION},
        json={"text": text, "incidentNames": incidents},
    )
    return [
        match["incidentName"]
        for match in (_json(response).get("incidentMatches") or [])
        if isinstance(match, dict) and isinstance(match.get("incidentName"), str)
    ]


def incident_samples(data: dict[str, Any]) -> dict[str, list[str]]:
    """The texts each rapid incident is taught with: its category's examples, and for the first category also the
    custom-category showcase prompt.

    A rapid incident is a few-shot classifier: it reliably catches text close to its samples and can miss a different
    sentence (a live run caught both examples of an airport category but not its showcase prompt). Teaching it the text
    that must be blocked is how rapid incident response is meant to work; the allowed showcase still guards over-blocking."""
    categories = data["application_controls"]["custom_categories"]
    showcase = (data.get("showcase") or {}).get("custom_category_prompt") or ""
    return {
        category["name"]: list(dict.fromkeys([*category["examples"], *([showcase] if index == 0 and showcase else [])]))
        for index, category in enumerate(categories)
    }


def provision_rapid() -> list[dict[str, Any]]:
    data = profile()
    names = resource_names(data)
    categories = data["application_controls"]["custom_categories"]
    by_source_name = {item["source_name"]: item for item in names["incidents"]}
    samples = incident_samples(data)
    deployed = []
    with _client(content_safety_endpoint(), "https://cognitiveservices.azure.com/.default") as client:
        for category in categories:
            resource = by_source_name.get(category["name"])
            if not resource:
                raise ValueError(f"no rapid incident resource for custom category {category['name']}")
            incident = resource["name"]
            path = f"/contentsafety/text/incidents/{incident}"
            existing = _send(
                client,
                "GET",
                path,
                expected={200, 404},
                label=f"read rapid incident {incident}",
                params={"api-version": CONTENT_SAFETY_API_VERSION},
            )
            existing_texts = _existing_sample_texts(client, incident) if existing.status_code == 200 else set()
            existing_definition = (
                (_json(existing).get("incidentDefinition") or "").strip()
                if existing.status_code == 200
                else ""
            )
            desired_texts = set(samples[category["name"]])
            if existing.status_code == 200 and (
                existing_definition != category["definition"].strip()
                or existing_texts != desired_texts
            ):
                _send(
                    client,
                    "DELETE",
                    path,
                    expected={200, 202, 204, 404},
                    label=f"replace changed rapid incident {incident}",
                    params={"api-version": CONTENT_SAFETY_API_VERSION},
                )
                existing_texts = set()
            _send(
                client,
                "PATCH",
                path,
                expected={200, 201},
                label=f"create rapid incident {incident}",
                params={"api-version": CONTENT_SAFETY_API_VERSION},
                json={"incidentName": incident, "incidentDefinition": category["definition"]},
            )
            missing = [text for text in samples[category["name"]] if text not in existing_texts]
            if missing:
                _send(
                    client,
                    "POST",
                    f"{path}:addIncidentSamples",
                    expected={200, 201, 202},
                    label=f"add samples to rapid incident {incident}",
                    params={"api-version": CONTENT_SAFETY_API_VERSION},
                    json={"IncidentSamples": [{"text": text} for text in missing]},
                )
            _send(
                client,
                "POST",
                f"{path}:deploy",
                expected={200, 201, 202},
                label=f"deploy rapid incident {incident}",
                params={"api-version": CONTENT_SAFETY_API_VERSION},
                json={},
            )
            deployed.append(
                {
                    "name": incident,
                    "source_name": category["name"],
                    "samples": len(samples[category["name"]]),
                    "activation_terms": category["activation_terms"],
                }
            )

        # Active means every incident catches its first sample, and the first one also the showcase prompt it demonstrates.
        deadline = time.monotonic() + 240
        pending = [(by_source_name[category["name"]]["name"], category["examples"][0]) for category in categories]
        if categories and (data.get("showcase") or {}).get("custom_category_prompt"):
            pending.append((by_source_name[categories[0]["name"]]["name"], data["showcase"]["custom_category_prompt"]))
        while pending and time.monotonic() < deadline:
            pending = [(incident, text) for incident, text in pending
                       if incident not in detect_incidents(client, text, [incident])]
            if pending:
                time.sleep(10)
        if pending:
            raise RuntimeError(f"rapid incidents did not become active: {sorted({incident for incident, _ in pending})}")

        incident_names = [item["name"] for item in deployed]
        first = incident_names[0]
        showcase_candidates = activated_incidents(data["showcase"]["custom_category_prompt"], data)
        if first not in showcase_candidates:
            raise RuntimeError(f"custom-category showcase did not activate {first}")
        showcase_matches = detect_incidents(
            client,
            data["showcase"]["custom_category_prompt"],
            showcase_candidates,
        )
        if first not in showcase_matches:
            raise RuntimeError(f"custom-category showcase did not match {first}")
        allowed_candidates = activated_incidents(data["showcase"]["allowed_prompt"], data)
        allowed_matches = (
            detect_incidents(client, data["showcase"]["allowed_prompt"], allowed_candidates)
            if allowed_candidates
            else []
        )
        if allowed_matches:
            raise RuntimeError(f"allowed showcase matched rapid incidents: {allowed_matches}")
    return deployed


def verify_rapid() -> list[str]:
    data = profile()
    incident_names = [item["name"] for item in resource_names(data)["incidents"]]
    with _client(content_safety_endpoint(), "https://cognitiveservices.azure.com/.default") as client:
        first = incident_names[0]
        showcase_candidates = activated_incidents(data["showcase"]["custom_category_prompt"], data)
        if first not in showcase_candidates:
            raise RuntimeError(f"custom-category showcase did not activate {first}")
        matches = detect_incidents(client, data["showcase"]["custom_category_prompt"], showcase_candidates)
        if first not in matches:
            raise RuntimeError(f"custom-category showcase did not match {first}")
        allowed_candidates = activated_incidents(data["showcase"]["allowed_prompt"], data)
        allowed = (
            detect_incidents(client, data["showcase"]["allowed_prompt"], allowed_candidates)
            if allowed_candidates
            else []
        )
        if allowed:
            raise RuntimeError(f"allowed showcase matched rapid incidents: {allowed}")
    return matches


def _state_path(value: str | None) -> pathlib.Path:
    path = pathlib.Path(value) if value else ROOT / ".foundry" / "content-safety.json"
    return path if path.is_absolute() else ROOT / path


def _write_state(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _read_state(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Content Safety state at {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def provision(target_project_id: str | None, state_file: str | None) -> dict[str, Any]:
    target = project_id(target_project_id)
    policy_id = configure_manifest(target)
    provision_arm(target)
    policy = verify_arm(target)
    incidents = provision_rapid()
    names = resource_names()
    state = {
        "schema_version": 1,
        "status": "provisioned",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id(target),
        "rai_policy": {"name": names["policy"], "id": policy_id, "mode": (policy.get("properties") or {}).get("mode")},
        "blocklist": {
            "name": names["blocklist"],
            "items": len(profile()["application_controls"]["blocklist_terms"]),
            "attached_to": ["Prompt", "Completion"],
        },
        "rapid_incidents": incidents,
        "agent": None,
    }
    _write_state(_state_path(state_file), state)
    return state


def _agent_version(agent: dict[str, Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    env_key = f"AGENT_{spec()['agent_name'].upper().replace('-', '_')}_VERSION"
    env_version = (os.environ.get(env_key) or "").strip()
    if env_version:
        return env_version
    latest = (agent.get("versions") or {}).get("latest") or {}
    value = latest.get("version") or agent.get("version")
    if value is None:
        raise RuntimeError("could not resolve the deployed agent version")
    return str(value)


def _content_filter_error(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    try:
        payload = response.json()
    except ValueError:
        return "content_filter" in response.text
    error = payload.get("error") if isinstance(payload, dict) else None
    return isinstance(error, dict) and error.get("code") == "content_filter"


def _route_agent_version(
    client: httpx.Client,
    agent_name: str,
    version: str,
) -> dict[str, Any] | None:
    response = _send(
        client,
        "GET",
        f"/agents/{agent_name}",
        expected={200},
        label=f"read agent route for {agent_name}",
        params={"api-version": "v1"},
    )
    agent = _json(response)
    endpoint = agent.get("agent_endpoint") or {}
    selector = endpoint.get("version_selector") or {}
    rules = selector.get("version_selection_rules") or []
    if (
        len(rules) == 1
        and str(rules[0].get("agent_version")) == str(version)
        and rules[0].get("traffic_percentage") == 100
    ):
        return None
    previous = json.loads(json.dumps(endpoint)) if endpoint else None
    updated_endpoint = json.loads(json.dumps(endpoint))
    updated_endpoint["version_selector"] = {
        "version_selection_rules": [
            {
                "type": "FixedRatio",
                "agent_version": str(version),
                "traffic_percentage": 100,
            }
        ]
    }
    _send(
        client,
        "PATCH",
        f"/agents/{agent_name}",
        expected={200},
        label=f"route {agent_name} to v{version} for verification",
        params={"api-version": "v1"},
        headers={"Content-Type": "application/merge-patch+json"},
        json={"agent_endpoint": updated_endpoint},
    )
    return previous


def _restore_agent_route(
    client: httpx.Client,
    agent_name: str,
    previous: dict[str, Any] | None,
) -> None:
    if previous is None:
        return
    _send(
        client,
        "PATCH",
        f"/agents/{agent_name}",
        expected={200},
        label=f"restore agent route for {agent_name}",
        params={"api-version": "v1"},
        headers={"Content-Type": "application/merge-patch+json"},
        json={"agent_endpoint": previous},
    )


def _verify_protocol_block(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    label: str,
) -> str | None:
    for attempt in range(1, 11):
        response = client.post(url, json=payload)
        if _content_filter_error(response):
            return response.headers.get("x-ms-agent-version")
        if attempt == 10:
            raise RuntimeError(
                f"{label} did not block the exact-match term: HTTP {response.status_code} {response.text[:300]}"
            )
        time.sleep(15)
    raise AssertionError("unreachable")


def verify_agent(
    target_project_id: str | None,
    endpoint: str | None,
    version: str | None,
    state_file: str | None,
) -> dict[str, Any]:
    target = project_id(target_project_id)
    policy_id = configure_manifest(target)
    verify_arm(target)
    verify_rapid()
    agent_name = spec()["agent_name"]
    with _client(project_endpoint(endpoint), "https://ai.azure.com/.default") as client:
        agent = _json(
            _send(
                client,
                "GET",
                f"/agents/{agent_name}",
                expected={200},
                label=f"read agent {agent_name}",
                params={"api-version": "v1"},
            )
        )
        resolved_version = _agent_version(agent, version)
        detail = _json(
            _send(
                client,
                "GET",
                f"/agents/{agent_name}/versions/{resolved_version}",
                expected={200},
                label=f"read agent {agent_name} v{resolved_version}",
                params={"api-version": "v1"},
            )
        )
        definition = detail.get("definition") or {}
        rai = definition.get("rai_config") or {}
        if str(rai.get("rai_policy_name", "")).casefold() != policy_id.casefold():
            raise RuntimeError(f"agent v{resolved_version} is not attached to {policy_id}")
        moderation = rai.get("invocations_moderation") or {}
        expected_paths = {"$.input", "$.message", "$.prompt", "$.query"}
        if moderation.get("response_mode") != "streaming" or not expected_paths <= set(moderation.get("input_paths") or []):
            raise RuntimeError(f"agent v{resolved_version} has incomplete invocations moderation")
        selectors = moderation.get("stream_selectors") or []
        if not any(item.get("event_type") == "token" and item.get("text_field") == "delta" for item in selectors):
            raise RuntimeError(f"agent v{resolved_version} does not moderate streamed token deltas")

        previous_route = _route_agent_version(client, agent_name, resolved_version)
        try:
            term = profile()["application_controls"]["blocklist_terms"][0]
            responses_version = _verify_protocol_block(
                client,
                f"/agents/{agent_name}/endpoint/protocols/openai/responses?api-version=v1",
                {"input": term, "store": False},
                "Responses guardrail",
            )
            invocations_version = _verify_protocol_block(
                client,
                f"/agents/{agent_name}/endpoint/protocols/invocations?api-version=v1",
                {"input": term},
                "Invocations guardrail",
            )
        finally:
            _restore_agent_route(client, agent_name, previous_route)
    for observed in (responses_version, invocations_version):
        if observed and str(observed) != str(resolved_version):
            raise RuntimeError(f"guardrail test routed to agent v{observed}, expected v{resolved_version}")

    path = _state_path(state_file)
    state = _read_state(path)
    state.update(
        {
            "schema_version": 1,
            "status": "verified",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "account_id": account_id(target),
            "agent": {
                "name": agent_name,
                "version": str(resolved_version),
                "rai_policy_id": policy_id,
                "responses_blocked": True,
                "invocations_blocked": True,
            },
        }
    )
    _write_state(path, state)
    return state


def verify_resources(target_project_id: str | None) -> dict[str, Any]:
    target = project_id(target_project_id)
    policy = verify_arm(target)
    rapid_matches = verify_rapid()
    return {
        "account_id": account_id(target),
        "rai_policy": policy.get("id"),
        "rapid_matches": rapid_matches,
    }


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(prog="python scripts/content_safety_ops.py")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("provision", "configure-manifest", "verify-resources", "verify-agent"):
        item = sub.add_parser(command)
        item.add_argument("--project-id")
        if command in ("provision", "verify-agent"):
            item.add_argument("--state-file")
        if command == "verify-agent":
            item.add_argument("--endpoint")
            item.add_argument("--version")
    args = parser.parse_args()
    if args.command == "configure-manifest":
        print(configure_manifest(args.project_id))
    elif args.command == "verify-resources":
        result = verify_resources(args.project_id)
        print("CONTENT_SAFETY_RESOURCES_VERIFIED " + json.dumps(result, sort_keys=True))
    elif args.command == "provision":
        result = provision(args.project_id, args.state_file)
        print(
            "CONTENT_SAFETY_PROVISIONED "
            f"policy={result['rai_policy']['name']} "
            f"blocklist={result['blocklist']['name']} "
            f"rapid={len(result['rapid_incidents'])}"
        )
    else:
        result = verify_agent(args.project_id, args.endpoint, args.version, args.state_file)
        print(
            "CONTENT_SAFETY_VERIFIED "
            f"agent={result['agent']['name']} "
            f"version={result['agent']['version']} "
            f"policy={result['rai_policy']['name']}"
        )


if __name__ == "__main__":
    main()
