"""Convert the seed evaluation set into AgentOps dataset rows.

AgentOps reads ``input``/``expected``/``context`` plus ``tool_definitions``;
the seed keeps the Foundry field names (``query``/``ground_truth``).
"""
import ast
import json
import sys
from pathlib import Path

SEED = Path("evaluations/public-library-3d1-seed-v1.jsonl")
OUT = Path("evaluations/agentops-dataset.jsonl")


def listish(value):
    if isinstance(value, str) and value.strip()[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return ast.literal_eval(value)
    return value


def convert(row: dict) -> dict:
    out = {"input": row["query"], "expected": row["ground_truth"], "context": row.get("context")}
    if row.get("tool_definitions"):
        out["tool_definitions"] = listish(row["tool_definitions"])
    if row.get("expected_behavior"):
        out["expected_behavior"] = row["expected_behavior"]
    return out


def render() -> str:
    rows = [json.loads(line) for line in SEED.read_text().splitlines() if line.strip()]
    return "".join(json.dumps(convert(r), ensure_ascii=False) + "\n" for r in rows)


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        sys.exit(0 if OUT.read_text() == render() else "evaluations/agentops-dataset.jsonl is stale; run python ci/build_dataset.py")
    OUT.write_text(render())
    print(f"Wrote {OUT}")
