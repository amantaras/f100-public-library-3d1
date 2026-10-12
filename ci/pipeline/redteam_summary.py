"""Render an AgentOps red-team summary as a Markdown table (counts only, no attack content)."""
import json
import sys
from pathlib import Path


def render(summary: dict) -> str:
    ok = not summary.get("has_violations")
    lines = [f"## {'✅' if ok else '⛔'} AI red team: {summary.get('successful_attacks', 0)} of "
             f"{summary.get('total_attempts', 0)} attacks succeeded "
             f"({summary.get('attack_success_rate', 0):.1%}; gate {summary.get('fail_threshold', 0):.0%})", "",
             "| Risk category | Successful | Attempts |", "| --- | ---: | ---: |"]
    for name, row in sorted((summary.get("per_category") or {}).items()):
        lines.append(f"| {name.replace('_', ' ')} | {row.get('successful', 0)} | {row.get('total', 0)} |")
    lines += ["", f"Attack strategies: {', '.join(summary.get('attack_strategies') or [])}. Raw prompts are not published."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    path = Path(sys.argv[1])
    print(render(json.loads(path.read_text())) if path.exists() else "## AI red team: no summary produced\n")
