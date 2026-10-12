"""Gate on evidence completeness, not merely Doctor's severity-based exit code."""

import argparse
import json
from pathlib import Path


def check(evidence: dict) -> None:
    if evidence.get("version") != 1 or evidence.get("status") not in {"ready", "ready_with_warnings"}:
        raise ValueError(f"Release readiness is blocked or unknown: {evidence.get('blockers', [])}")
    checks = {item["name"]: item for item in evidence.get("checks", [])}
    for name in ("Latest eval gate", "Regression baseline", "Runtime monitoring"):
        if checks.get(name, {}).get("status") != "ready":
            raise ValueError(f"Missing required readiness evidence: {name}")
    foundry = checks.get("Foundry continuous evaluation", {}).get("evidence", {})
    if foundry.get("status") != "ok":
        raise ValueError("Foundry source is unavailable; cannot establish readiness")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    check(json.loads(args.evidence.read_text()))
    print("Required release evidence is present; review recorded warnings before production use.")
