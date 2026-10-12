"""Render the production release record as GitHub Summary markdown (read-only)."""

import json
from pathlib import Path
import sys


def render(record_path: Path, evidence_path: Path) -> str:
    lines = ["## Production release"]
    if record_path.exists():
        record = json.loads(record_path.read_text())
        identity, toolbox = record["identity"], record["toolbox"]
        lines += [
            f"- **Status:** `{record['status']}`",
            f"- **Production version:** `{record.get('selected_version', record['candidate_version'])}` "
            f"(previous `{record['previous_version']}`)",
            f"- **Evaluated in dev as:** v{record['evaluated_in']['version']}",
            f"- **Artifact:** `{identity['artifact'][:16]}` from revision `{identity['deployed_git_sha'][:12]}`",
            f"- **Toolbox:** `{toolbox['toolbox']}` content `{toolbox['toolbox_sha256'][:16]}` (matches dev)",
        ]
        if record.get("smoke"):
            lines.append(f"- **Smoke:** routing tools {record['smoke']['routing_tools']}; "
                         f"tools exposed {record['smoke']['tools_exposed']}")
    else:
        lines.append("No verified production record was produced; this run did not change production routing.")
    if evidence_path.exists():
        lines += ["", evidence_path.read_text()]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render(Path(sys.argv[1]), Path(sys.argv[2])))
