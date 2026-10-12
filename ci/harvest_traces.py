"""Export production agent traces from Application Insights for `agentops eval promote-traces`.

Reads `invoke_agent` request spans (prompt/completion recorded by the runtime) from the
production Application Insights resource, excludes CI evaluation sessions, masks obvious
personal contact data, and writes JSONL in the shape AgentOps promotes into a reviewable
regression dataset. Rows are candidates for human review, never ground truth.
"""

import argparse
import json
import os
from pathlib import Path
import re

import httpx

from provenance import credential as get_credential

QUERY = """
requests
| where timestamp > ago({days}d)
| where name startswith 'invoke_agent' and cloud_RoleName == '{agent}'
| extend session = tostring(customDimensions['gen_ai.conversation.id']),
         prompt = tostring(customDimensions['gen_ai.prompt']),
         completion = tostring(customDimensions['gen_ai.completion']),
         agent_id = tostring(customDimensions['gen_ai.agent.id']),
         foundry_project = tostring(customDimensions['microsoft.foundry.project.id'])
| where isnotempty(prompt) and isnotempty(completion) and not(session startswith 'ci-')
| project timestamp, operation_Id, id, session, prompt, completion, agent_id, foundry_project, success, duration
| order by timestamp desc
| take {limit}
"""
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<![\w(])\+?\d[\d\s().-]{7,}\d(?!\w)")
KNOWN_SAFE = {"(888) 426-4435"}  # ASPCA Animal Poison Control, part of approved answers


def mask(text: str) -> str:
    text = EMAIL.sub("[email]", text)
    return PHONE.sub(lambda m: m.group(0) if m.group(0).strip() in KNOWN_SAFE else "[phone]", text)


def application_id(connection_string: str) -> str:
    parts = dict(part.split("=", 1) for part in connection_string.split(";") if "=" in part)
    if not parts.get("ApplicationId"):
        raise ValueError("Connection string has no ApplicationId")
    return parts["ApplicationId"]


def export(app_id: str, agent: str, days: int, limit: int) -> list[dict]:
    with get_credential() as credential:
        token = credential.get_token("https://api.applicationinsights.io/.default").token
    response = httpx.post(f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"query": QUERY.format(days=days, agent=agent, limit=limit)}, timeout=120)
    response.raise_for_status()
    table = response.json()["tables"][0]
    columns = [column["name"] for column in table["columns"]]
    rows = []
    for values in table["rows"]:
        record = dict(zip(columns, values))
        agent_name, _, version = record["agent_id"].partition(":")
        session = record["session"] or ""
        rows.append({
            "input": mask(record["prompt"]), "response": mask(record["completion"]),
            "trace_id": record["operation_Id"], "operation_Id": record["operation_Id"], "span_id": record["id"],
            "timestamp": record["timestamp"], "agent": agent_name, "agent_version": version,
            "foundry_project": record["foundry_project"], "source_system": "application_insights",
            "sampling_policy": "synthetic_traffic" if session.startswith("synthetic-") else "production_traffic",
        })
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default="public-library-3d1")
    parser.add_argument("--app-id", help="Application Insights ApplicationId (default: from connection string)")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    app_id = args.app_id or application_id(os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"])
    rows = export(app_id, args.agent, args.days, args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    print(f"Exported {len(rows)} production traces (CI sessions excluded) -> {args.output}")
