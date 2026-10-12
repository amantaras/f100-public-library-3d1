"""Block a release candidate that scores worse than the version in production.

AgentOps (``agentops eval run``) enforces absolute thresholds. This gate adds
the non-regression rule: compare the candidate's AgentOps results with the
results recorded when the current production version was released.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# A drop larger than this blocks the release. 1-5 judge scales tolerate half a
# point of judge noise; 0-1 pass rates tolerate two cases (never less than 5 points): one
# flipped case is sampling noise, and on a small dataset a single case moves a rate a lot.
TOLERANCE = {"scale": 0.5, "rate": 0.05}
RATE_CASES = 2
LOWER_IS_BETTER = {"avg_latency_seconds"}


def metrics(results: dict) -> dict[str, float]:
    return {k: float(v) for k, v in (results.get("aggregate_metrics") or {}).items() if isinstance(v, (int, float))}


def rate_tolerance(*results: dict) -> float:
    """Two cases of the smaller dataset, at least the base tolerance."""
    sizes = [r.get("summary", {}).get("items_total") for r in results]
    sizes = [s for s in sizes if isinstance(s, (int, float)) and s > 0]
    return max(TOLERANCE["rate"], RATE_CASES / min(sizes)) if sizes else TOLERANCE["rate"]


def compare(baseline: dict, candidate: dict) -> list[dict]:
    base, cand = metrics(baseline), metrics(candidate)
    rate = rate_tolerance(baseline, candidate)
    rows = []
    for name in sorted(set(base) & set(cand)):
        b, c = base[name], cand[name]
        if name in LOWER_IS_BETTER:
            # Latency varies with load; flag it but only block a doubling.
            worse = c > max(b * 2, b + 10)
            delta = c - b
        else:
            tolerance = rate if max(b, c) <= 1 else TOLERANCE["scale"]
            delta = c - b
            worse = delta < -tolerance
        rows.append({"metric": name, "production": b, "candidate": c, "delta": delta, "regressed": worse})
    return rows


def markdown(rows: list[dict], candidate: dict, baseline_label: str, passed: bool) -> str:
    lines = [f"### {'✅ No regression' if passed else '⛔ Regression: release blocked'} vs production ({baseline_label})", "",
             "| Metric | Production | Candidate | Change |", "| --- | ---: | ---: | ---: |"]
    for r in rows:
        mark = " ⛔" if r["regressed"] else ""
        lines.append(f"| {r['metric']} | {r['production']:.2f} | {r['candidate']:.2f} | {r['delta']:+.2f}{mark} |")
    s = candidate.get("summary") or {}
    lines += ["", f"AgentOps thresholds: {s.get('thresholds_passed')}/{s.get('thresholds_total')} passed; "
              f"cases passing all checks: {s.get('items_passed_all')}/{s.get('items_total')}."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, help="Production results; omitted on the very first release")
    parser.add_argument("--baseline-label", default="production")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path, help="Also write the verdict as Markdown (for the PR comment)")
    args = parser.parse_args()
    candidate = json.loads(args.candidate.read_text())
    if not args.baseline or not args.baseline.exists():
        text = "### First release: no production baseline yet; AgentOps thresholds decide.\n"
        rows, passed = [], True
    else:
        rows = compare(json.loads(args.baseline.read_text()), candidate)
        passed = not any(r["regressed"] for r in rows)
        text = markdown(rows, candidate, args.baseline_label, passed)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(text)
    if args.markdown:
        args.markdown.write_text(text)
    if args.output:
        args.output.write_text(json.dumps({"passed": passed, "metrics": rows}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
