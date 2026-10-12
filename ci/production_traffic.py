"""Send synthetic, realistic user questions to the production agent to create production traces.

This simulates real usage for the showcase. Questions are deliberately different from the
release evaluation dataset so harvested traces add new regression candidates. Sessions are
tagged ``synthetic-`` so harvesting can distinguish them from CI traffic (``ci-``).
"""

import argparse
import json
from pathlib import Path
import random
import uuid

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import VersionRefIndicator
import httpx

from evaluate import parse_stream_events
from provenance import credential as get_credential
from release import route

QUESTIONS = json.loads((Path(__file__).resolve().parent / "production_queries.json").read_text())


def ask(project, credential, endpoint: str, agent: str, version: str, question: str) -> dict:
    session_id = f"synthetic-{uuid.uuid4().hex}"
    project.beta.agents.create_session(agent, version_indicator=VersionRefIndicator(agent_version=version),
                                       agent_session_id=session_id)
    try:
        token = credential.get_token("https://ai.azure.com/.default").token
        response = httpx.post(
            f"{endpoint.rstrip('/')}/agents/{agent}/endpoint/protocols/invocations",
            params={"api-version": "v1", "agent_session_id": session_id},
            headers={"Authorization": f"Bearer {token}"},
            json={"input": question, "include_tool_events": True, "memory": False}, timeout=180,
        )
        response.raise_for_status()
        observed = parse_stream_events(response.text)
        return {"session_id": session_id, "question": question,
                "tools": [call["name"] for call in observed["tool_calls"]], "characters": len(observed["text"])}
    finally:
        project.beta.agents.delete_session(agent, session_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--agent", default="public-library-3d1")
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    questions = random.Random(args.seed).sample(QUESTIONS, min(args.count, len(QUESTIONS)))
    with get_credential() as credential, AIProjectClient(
        endpoint=args.endpoint, credential=credential, allow_preview=True
    ) as project:
        version = route(project, args.agent)
        print(f"Sending {len(questions)} synthetic questions to {args.agent}:v{version} (serving route)")
        for question in questions:
            print(json.dumps(ask(project, credential, args.endpoint, args.agent, version, question)), flush=True)
