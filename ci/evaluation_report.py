"""Render actual evaluation evidence for people; never score responses or approve a release."""

import argparse
from datetime import datetime, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
from string import Template
from urllib.parse import urlsplit

from quality_gate import METRICS, compare, load_rows, tool_route_match

TEMPLATE = Path(__file__).with_name("evaluation_report.html")
LABELS = {
    "relevance": "Relevance", "expected_behavior": "Expected behavior",
    "indirect_attack": "Indirect attack resistance", "task_adherence": "Task adherence",
    "task_completion": "Task completion", "intent_resolution": "Intent resolution",
    "domain_rubric": "Domain rubric", "tool_call_accuracy": "Tool call accuracy",
    "tool_selection": "Tool selection", "tool_input_accuracy": "Tool input accuracy",
    "tool_output_utilization": "Tool output utilization", "tool_call_success": "Tool call success",
    "tool_route_match": "Expected tool route (deterministic)",
}
GROUPS = {
    "Answer quality": ("relevance", "expected_behavior", "domain_rubric"),
    "Agent behavior": ("task_adherence", "task_completion", "intent_resolution"),
    "Tool use": ("tool_route_match", "tool_call_accuracy", "tool_selection", "tool_input_accuracy",
                 "tool_output_utilization", "tool_call_success"),
    "Safety": ("indirect_attack",),
}
GATE_LABELS = {"case": "Per-case, strict", "aggregate": "Aggregate, one-case tolerance"}
FAILURE_LABELS = {
    "evaluation_authentication": "Scoring authentication or permissions failed. This is not an agent-quality verdict.",
    "evaluation_service": "The scoring service failed before a reliable quality verdict was available.",
    "evaluation_timeout": "Scoring timed out; complete evaluation evidence is unavailable.",
    "incomplete_evaluation": "The scoring service did not expose every required result.",
    "evaluation_contract": "The scorer output did not match the declared evaluation contract.",
    "agent_execution": "An agent invocation failed before it could be scored.",
}


def safe_url(value) -> str:
    if (not isinstance(value, str) or any(character.isspace() for character in value)
            or any(character in value for character in '<>"\'\\`()')):
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme != "https" or parsed.hostname not in {"ai.azure.com", "github.com"}
            or parsed.username or parsed.password or port not in {None, 443}):
        return ""
    return value if not any(token in parsed.query.lower() for token in ("sig=", "token=", "key=")) else ""


def fmt(value, percent=False) -> str:
    if type(value) not in {int, float} or not math.isfinite(value):
        return "Not recorded"
    return f"{value:.1%}" if percent else f"{value:.2f}"


def source(item: dict) -> dict:
    value = item["datasource_item"]
    return value.get("item", value)


def concise_reason(result: dict) -> str:
    reason = result.get("reason")
    return reason if isinstance(reason, str) and reason.strip() else "No concise explanation was supplied by the grader."


def grade_text(score, passed) -> str:
    if score is None:
        return "N/A"
    return f"{fmt(score)} ({'pass' if passed else 'fail'})"


def item_grades(item: dict, row: dict) -> dict:
    grades = {grade["name"]: grade for grade in item["results"]}
    route = tool_route_match(row, source(item))
    grades["tool_route_match"] = {
        "name": "tool_route_match", "score": route, "passed": bool(route),
        "reason": "Expected tools: {}. Observed tools: {}.".format(
            ", ".join(sorted({c["name"] for c in row.get("tool_calls") or []})) or "none",
            ", ".join(call["name"] for call in source(item).get("tool_calls") or []) or "none"),
    }
    return grades


def applicable_summary(report: dict) -> dict:
    """Each side's own mean over its applicable cases (display only; the gate uses paired cases)."""
    rows = load_rows()
    values: dict[str, list[float]] = {name: [] for name in METRICS}
    for item in report["output_items"]:
        for name, grade in item_grades(item, rows[int(source(item)["case_id"])]).items():
            if grade["score"] is not None:
                values[name].append(grade["score"])
    return {name: {"mean": sum(v) / len(v) if v else None, "cases": len(v)} for name, v in values.items()}


def unpaired_text(summary: dict) -> str:
    if summary["mean"] is None:
        return "N/A (0 applicable)"
    return f"{fmt(summary['mean'])} (unpaired, n={summary['cases']})"


def tool_trace(item: dict) -> list[dict]:
    return [{"name": call.get("name"), "arguments": json.dumps(call.get("arguments"), ensure_ascii=False)}
            for call in source(item).get("tool_calls") or []]


def case_comparisons(baseline: dict, candidate: dict) -> list[dict]:
    rows = load_rows()
    before = {source(item)["case_id"]: item for item in baseline["output_items"]}
    after = {source(item)["case_id"]: item for item in candidate["output_items"]}
    cases = []
    for case_id in sorted(before, key=int):
        old, new = before[case_id], after[case_id]
        row = rows[int(case_id)]
        old_grades, new_grades = item_grades(old, row), item_grades(new, row)
        paired = [name for name in METRICS
                  if old_grades[name]["score"] is not None and new_grades[name]["score"] is not None]
        regression = any(new_grades[name]["score"] < old_grades[name]["score"]
                         or (old_grades[name]["passed"] and not new_grades[name]["passed"]) for name in paired)
        improvement = any(new_grades[name]["score"] > old_grades[name]["score"]
                          or (not old_grades[name]["passed"] and new_grades[name]["passed"]) for name in paired)
        cases.append({
            "id": case_id, "query": source(new)["query"], "expected": source(new)["expected_behavior"],
            "category": row.get("category", "uncategorized"),
            "baseline_tools": tool_trace(old), "candidate_tools": tool_trace(new),
            "baseline_response": source(old)["response"], "candidate_response": source(new)["response"],
            "baseline_elapsed": baseline.get("provenance", {}).get("case_elapsed_seconds", {}).get(case_id),
            "candidate_elapsed": candidate.get("provenance", {}).get("case_elapsed_seconds", {}).get(case_id),
            "baseline_passed": all(grade["passed"] is not False for grade in old_grades.values()),
            "candidate_passed": all(grade["passed"] is not False for grade in new_grades.values()),
            "change": "regressed" if regression else "improved" if improvement else "unchanged",
            "grades": [{
                "name": name, "gate": METRICS[name]["gate"], "baseline_score": old_grades[name]["score"],
                "candidate_score": new_grades[name]["score"],
                "baseline_passed": old_grades[name]["passed"], "candidate_passed": new_grades[name]["passed"],
                "baseline_reason": concise_reason(old_grades[name]), "candidate_reason": concise_reason(new_grades[name]),
            } for name in METRICS],
        })
    return cases


def build_model(baseline: dict | None, candidate: dict | None, gate: dict | None,
                deployment: dict | None = None, preflight_status="", notes=None) -> dict:
    model = {
        "state": "unavailable", "title": "No completed evaluation comparison",
        "message": "No release approval can be inferred from missing or incomplete evidence.",
        "baseline": baseline or {}, "candidate": candidate or {}, "gate": gate or {},
        "deployment": deployment or {}, "cases": [], "notes": list(notes or []),
        "metrics": {}, "preflight_status": preflight_status,
    }
    if deployment and deployment.get("status") == "unchanged":
        same_version = deployment.get("baseline_version") == deployment.get("candidate_version")
        if (not re.fullmatch(r"[1-9][0-9]*", str(deployment.get("candidate_version", "")))
                or not (same_version or deployment.get("equivalent_artifact") is True)
                or deployment.get("evaluation_performed") is not False
                or deployment.get("promotion_performed") is not False):
            model.update(state="error", title="Invalid no-change evidence")
            return model
        model.update(
            state="unchanged", title="No change - no new agent evaluation",
            message=(f"Version {deployment['candidate_version']} already represents this source revision. "
                     if same_version else
                     f"Version {deployment['candidate_version']} is byte-identical to serving version "
                     f"{deployment['baseline_version']} (same source content hash and definition); "
                     "only non-agent files changed. ")
                    + "No new agent evaluation or promotion was performed; no fresh quality scores are claimed.",
            baseline={}, candidate={}, gate={},
        )
        return model
    for name, report in (("Candidate", candidate), ("Baseline", baseline)):
        if report and (report.get("failure_kind") or (report.get("run") or {}).get("status") != "completed"):
            kind = report.get("failure_kind", "evaluation_service")
            model.update(state="error", title=f"{name} evaluation did not complete",
                         message=FAILURE_LABELS.get(kind, "Evaluation execution failed; inspect the raw artifacts."))
            return model
    if not baseline or not candidate or not gate:
        if preflight_status == "failure":
            model.update(state="error", title="Scoring preflight failed",
                         message="Scoring access or calibration failed before agent deployment. "
                                 "Inspect the preflight artifact; do not diagnose this as an agent-quality regression.")
        return model
    try:
        expected = compare(baseline, candidate)
        if (type(gate.get("passed")) is not bool or gate["passed"] != expected["passed"]
                or gate.get("metrics") != expected["metrics"]
                or gate.get("failures") != expected["failures"]):
            raise ValueError("Stored gate decision differs from the validated scored evidence")
        model["cases"] = case_comparisons(baseline, candidate)
    except (KeyError, TypeError, ValueError) as exc:
        model.update(state="error", title="Invalid or incomparable evaluation evidence",
                     message="This comparison cannot establish release readiness. "
                             "Check the dataset/evaluator/policy fingerprints and raw results.")
        model["notes"].append(f"Evidence validation: {type(exc).__name__}")
        return model
    model["metrics"] = expected["metrics"]
    model["applicable"] = {"baseline": applicable_summary(baseline), "candidate": applicable_summary(candidate)}
    if expected["passed"]:
        model.update(state="passed", title="Evaluation gate passed - no measured regression",
                     message="The candidate met the baseline non-regression policy. "
                             "A pass does not mean every answer is correct, and is not proof that promotion completed.")
    else:
        model.update(state="blocked", title="Evaluation gate blocked the candidate",
                     message="Completed scores did not satisfy the release policy. "
                             "Inspect the regressed cases and gate reasons; no release approval is inferred.")
    return model


def report_link(report: dict) -> str:
    return safe_url((report.get("run") or {}).get("report_url"))


def version(report: dict) -> str:
    value = (report.get("provenance") or {}).get("version")
    return str(value) if re.fullmatch(r"[1-9][0-9]*", str(value)) else "Not recorded"


def markdown_report(model: dict, run_url="") -> str:
    lines = [f"## Evaluation report: {model['title']}", "", model["message"], ""]
    if safe_url(run_url):
        lines.append(f"[This GitHub run](<{safe_url(run_url)}>)")
    for name in ("baseline", "candidate"):
        link = report_link(model[name])
        if link:
            lines.append(f"[{name.title()} v{version(model[name])} - Foundry evaluation](<{link}>)")
    if model["cases"]:
        cases = model["cases"]
        total = len(cases)
        passed_before = sum(case["baseline_passed"] for case in cases)
        passed_after = sum(case["candidate_passed"] for case in cases)
        regressed = sum(case["change"] == "regressed" for case in cases)
        improved = sum(case["change"] == "improved" for case in cases)
        lines.extend([
            "", f"**All-grader case passes:** baseline **{passed_before}/{total}** -> candidate **{passed_after}/{total}**.",
            f"**Changes:** {improved} improved, {regressed} regressed, {total - improved - regressed} unchanged.",
            "", "| Group | Metric | Gate | Paired cases | Baseline mean | Candidate mean | Delta | "
            "Baseline pass rate | Candidate pass rate | Result |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
        ])
        for group, names in GROUPS.items():
            for name in names:
                metric = model["metrics"].get(name)
                if not metric:
                    continue
                comparable = metric["status"] != "not_comparable"
                delta = f"{metric['candidate_mean'] - metric['baseline_mean']:+.2f}" if comparable else "N/A"
                old_mean, new_mean = (fmt(metric["baseline_mean"]), fmt(metric["candidate_mean"])) if comparable else (
                    unpaired_text(model["applicable"]["baseline"][name]),
                    unpaired_text(model["applicable"]["candidate"][name]))
                lines.append(
                    f"| {group} | {LABELS.get(name, name)} | {GATE_LABELS[metric['gate']]} | "
                    f"{metric['paired_cases']} | {old_mean} | {new_mean} | "
                    f"{delta} | {fmt(metric['baseline_pass_rate'], True)} | {fmt(metric['candidate_pass_rate'], True)} | "
                    f"{metric_status(metric)} |")
        lines.append("\nTool metrics are compared only on cases where both versions actually called a tool; "
                     "`N/A` means the evaluator reported *not applicable* (no observed tool call), never an invented score. "
                     "*Unpaired* means are each version's own applicable cases, shown for visibility only; "
                     "they are not used by the gate.")
        lines.extend(["", "### Case results", "",
                      "| Case ID | Category | Candidate tools | Baseline | Candidate | Change | Failed candidate checks |",
                      "|---|---|---|---|---|---|---|"])
        for case in cases:
            failed = ", ".join(grade["name"] for grade in case["grades"] if grade["candidate_passed"] is False) or "None"
            tools = ", ".join(f"`{call['name']}`" for call in case["candidate_tools"]) or "none"
            lines.append(f"| {case['id']} | {case['category']} | {tools} | {'Pass' if case['baseline_passed'] else 'Fail'} | "
                         f"{'Pass' if case['candidate_passed'] else 'Fail'} | {case['change'].title()} | {failed} |")
        if passed_after < total:
            lines.append("\n**Existing failures remain visible:** an unchanged failing case can coexist with a passed "
                         "non-regression gate. That is not a claim of perfect or clinically validated answers.")
        if model["gate"]["failures"]:
            lines.extend(["", "### Why the release was blocked",
                          *[f"- `{reason}`" for reason in model["gate"]["failures"]]])
        if model["gate"].get("watch"):
            lines.extend(["", "### Per-case movements within aggregate tolerance (review, not blocking)",
                          *[f"- `{reason}`" for reason in model["gate"]["watch"]]])
    else:
        lines.append("\nNo scored baseline/candidate comparison is presented for this run.")
    lines.extend([
        "", "### Detailed report",
        "Download this run's **eval-report-...** artifact and open **evaluation-report.html** "
        "for side-by-side answers, reference behavior, grader explanations and provenance. "
        "Raw scored evidence remains in the separate **evaluation-...** artifact.",
        "", "Prompts, responses and grader text are intentionally omitted from this Summary. "
        "They remain in the restricted downloadable report. Case IDs are zero-based.",
    ])
    if model["notes"]:
        lines.extend(["", "### Evidence availability", *[f"- {note}" for note in model["notes"]]])
    return "\n".join(lines) + "\n"


def metric_status(metric: dict) -> str:
    return {"pass": "Pass", "regression": "Regression", "not_comparable": "Not comparable (N/A)"}[metric["status"]]


def html_tools(calls: list[dict]) -> str:
    if not calls:
        return '<p class="muted">No tool call observed.</p>'
    return "<ol>" + "".join(f'<li><code>{escape(str(call["name"]))}</code> '
                            f'<span class="muted">{escape(call["arguments"])}</span></li>' for call in calls) + "</ol>"


def html_link(label: str, url: str) -> str:
    return f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a>' if safe_url(url) else ""


def html_report(model: dict, run_url="") -> str:
    links = [html_link("GitHub run", run_url)]
    links.extend(html_link(f"{name.title()} v{version(model[name])} in Foundry", report_link(model[name]))
                 for name in ("baseline", "candidate"))
    navigation = " ".join(link for link in links if link)
    cards, overview, details, metrics = [], [], [], []
    cases = model["cases"]
    if cases:
        total = len(cases)
        before = sum(case["baseline_passed"] for case in cases)
        after = sum(case["candidate_passed"] for case in cases)
        regressed = sum(case["change"] == "regressed" for case in cases)
        improved = sum(case["change"] == "improved" for case in cases)
        for label, value, caption in (
            (f"Baseline v{version(model['baseline'])}", f"{before}/{total}", "Cases passing every grader"),
            (f"Candidate v{version(model['candidate'])}", f"{after}/{total}", "Cases passing every grader"),
            ("Case movement", f"{improved} improved", f"{regressed} regressed; {total - improved - regressed} unchanged"),
            ("Still failing", str(total - after), "Candidate cases requiring review; not hidden by a passing gate"),
        ):
            cards.append(f'<article class="card"><p class="eyebrow">{escape(label)}</p>'
                         f'<strong class="big">{escape(value)}</strong><p>{escape(caption)}</p></article>')
        for group, names in GROUPS.items():
            panels = []
            for name in names:
                metric = model["metrics"].get(name)
                if not metric:
                    continue
                low, high = METRICS[name]["range"]
                bars = ""
                for side in ("baseline", "candidate"):
                    mean = metric[f"{side}_mean"]
                    if metric["status"] == "not_comparable":
                        mean = model["applicable"][side][name]["mean"]
                    width = 0 if mean is None else max(0, min(100, (mean - low) / (high - low) * 100))
                    bars += (f'<div class="bar-row"><span>{side.title()}</span><div class="track">'
                             f'<div class="bar {side}" style="width:{width:.2f}%"></div></div>'
                             f'<strong>{fmt(mean) if mean is not None else "N/A"}</strong></div>')
                if metric["status"] == "not_comparable":
                    summary = ("No paired applicable cases yet, so this metric is not used for this decision. "
                               f"Unpaired: baseline {unpaired_text(model['applicable']['baseline'][name])}, "
                               f"candidate {unpaired_text(model['applicable']['candidate'][name])}.")
                else:
                    summary = (f'Mean delta: <strong>{metric["candidate_mean"] - metric["baseline_mean"]:+.2f}</strong>'
                               f' &middot; Pass rates: {fmt(metric["baseline_pass_rate"], True)} &rarr; '
                               f'{fmt(metric["candidate_pass_rate"], True)}')
                panels.append(
                    f'<article class="panel"><h3>{escape(LABELS.get(name, name))} <small>scale {low}-{high}</small></h3>'
                    f'{bars}<p>{summary}</p><p class="muted">{escape(GATE_LABELS[metric["gate"]])} &middot; '
                    f'{metric["paired_cases"]} paired cases &middot; applicable baseline {metric["baseline_applicable"]} / '
                    f'candidate {metric["candidate_applicable"]} &middot; <span class="tag '
                    f'{"regressed" if metric["status"] == "regression" else "unchanged"}">{escape(metric_status(metric))}</span></p></article>')
            if panels:
                metrics.append(f'<h3 class="group">{escape(group)}</h3><div class="metric-grid">{"".join(panels)}</div>')
        for case in cases:
            case_id = case["id"]
            failed = ", ".join(LABELS[g["name"]] for g in case["grades"] if g["candidate_passed"] is False) or "None"
            tools = ", ".join(call["name"] for call in case["candidate_tools"]) or "none"
            overview.append(f'<tr><td><a href="#case-{case_id}">Case {case_id}</a></td>'
                            f'<td>{escape(case["category"])}</td><td><code>{escape(tools)}</code></td>'
                            f'<td>{"Pass" if case["baseline_passed"] else "Fail"}</td>'
                            f'<td>{"Pass" if case["candidate_passed"] else "Fail"}</td>'
                            f'<td><span class="tag {case["change"]}">{case["change"].title()}</span></td>'
                            f'<td>{escape(failed)}</td></tr>')
            grade_rows = "".join(
                f'<tr><td>{escape(LABELS[g["name"]])}</td><td>{escape(GATE_LABELS[g["gate"]])}</td>'
                f'<td>{grade_text(g["baseline_score"], g["baseline_passed"])}</td>'
                f'<td>{grade_text(g["candidate_score"], g["candidate_passed"])}</td></tr>' for g in case["grades"]
            )
            reasons = "".join(
                f'<article class="reason"><h4>{escape(LABELS[g["name"]])}</h4>'
                f'<p><strong>Baseline judge:</strong> {escape(g["baseline_reason"])}</p>'
                f'<p><strong>Candidate judge:</strong> {escape(g["candidate_reason"])}</p></article>' for g in case["grades"]
            )
            opened = " open" if not case["candidate_passed"] or case["change"] == "regressed" else ""
            details.append(
                f'<details class="case" id="case-{case_id}"{opened}><summary><strong>Case {case_id}</strong>'
                f'<span class="tag {case["change"]}">{case["change"].title()}</span>'
                f'<span class="tag unchanged">{escape(case["category"])}</span>'
                f'<span>{escape(case["query"])}</span></summary><div class="case-body">'
                f'<h3>Reference behavior</h3><p class="response">{escape(case["expected"])}</p>'
                f'<div class="answer-grid"><section><h3>Baseline answer</h3>'
                f'<div class="response">{escape(case["baseline_response"])}</div></section>'
                f'<section><h3>Candidate answer</h3><div class="response">{escape(case["candidate_response"])}</div>'
                f'</section></div><p class="muted">Case wall time in seconds, including session setup and cleanup: '
                f'baseline {fmt(case["baseline_elapsed"])} / candidate {fmt(case["candidate_elapsed"])}.</p>'
                f'<h3>Observed tool calls</h3><div class="answer-grid"><section><h4>Baseline</h4>{html_tools(case["baseline_tools"])}'
                f'</section><section><h4>Candidate</h4>{html_tools(case["candidate_tools"])}</section></div>'
                f'<div class="table-wrap"><table><thead><tr><th>Evaluator</th><th>Gate</th><th>Baseline</th>'
                f'<th>Candidate</th></tr></thead><tbody>{grade_rows}</tbody></table></div>'
                f'<h3>Why the graders decided this</h3>{reasons}</div></details>'
            )
    else:
        cards.append('<article class="panel"><h2>No fresh quality comparison</h2>'
                     '<p>No pass rate, case movement or approval is invented for missing, failed or unchanged-run evidence.</p></article>')
    context = []
    for name in ("baseline", "candidate"):
        report = model[name]
        if not report or model["state"] == "unchanged":
            continue
        run = report.get("run") or {}
        provenance = report.get("provenance") or {}
        rows = [("Agent", provenance.get("agent")), ("Version", provenance.get("version")),
                ("Execution status", run.get("status")), ("Evaluation ID", run.get("eval_id")),
                ("Run ID", run.get("id")), ("Scoring duration (service)", run.get("duration")),
                ("Evaluated source revision", provenance.get("evaluation_git_sha")),
                ("Artifact", provenance.get("artifact")), ("Dataset SHA256", provenance.get("dataset_sha256")),
                ("Deployed configuration SHA256", provenance.get("definition_sha256")),
                ("Criteria SHA256", provenance.get("criteria_sha256")),
                ("Evaluator SHA256", provenance.get("evaluator_sha256")),
                ("Instructions SHA256", provenance.get("instructions_sha256")),
                ("Policy SHA256", provenance.get("policy_sha256"))]
        context.append(f'<section class="panel"><h3>{name.title()} evidence</h3><dl>' +
                       "".join(f'<dt>{escape(label)}</dt><dd><code>{escape(str(value)) if value is not None else "Not recorded"}</code></dd>'
                               for label, value in rows) + "</dl></section>")
    notices = "".join(f"<li>{escape(note)}</li>" for note in model["notes"])
    if model["state"] == "unchanged":
        decision = model["deployment"]
        context.append('<section class="panel"><h3>Verified no-change record</h3><dl>' + "".join(
            f'<dt>{escape(label)}</dt><dd><code>{escape(str(value))}</code></dd>'
            for label, value in (
                ("Selected version", decision["candidate_version"]),
                ("Source revision", (decision.get("identity") or {}).get("deployed_git_sha", "Not recorded")),
                ("Artifact", (decision.get("identity") or {}).get("artifact", "Not recorded")),
                ("Agent evaluation performed", False), ("Promotion performed", False),
            )
        ) + "</dl></section>")
    if not context:
        context.append('<p>See the deployment decision or workflow logs for this non-evaluation run.</p>')
    return Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        agent=escape(str((model["candidate"].get("provenance") or model["baseline"].get("provenance") or {})
                         .get("agent", model["deployment"].get("agent", "Agent not recorded")))),
        title=escape(model["title"]), state=escape(model["state"]),
        message=escape(model["message"]), navigation=navigation,
        gate_reasons=('<section class="panel"><h2>Why the release was blocked</h2><ul>' +
                      "".join(f"<li>{escape(reason)}</li>" for reason in model["gate"]["failures"]) +
                      "</ul></section>") if cases and model["gate"]["failures"] else "",
        generated=escape(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        cards="".join(cards), metrics="".join(metrics),
        overview="".join(overview) or '<tr><td colspan="7">No comparable scored cases.</td></tr>',
        details="".join(details), context="".join(context),
        notices=f'<section class="panel"><h2>Evidence availability</h2><ul>{notices}</ul></section>' if notices else "",
    )


def load_optional(path: Path, notes: list[str]) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        notes.append(f"{path.name} could not be decoded; no result was inferred.")
        return None
    if not isinstance(value, dict):
        notes.append(f"{path.name} has an invalid report shape.")
        return None
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--deployment-decision", type=Path)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--append-summary", action="store_true")
    args = parser.parse_args()
    notes = []
    inputs = {name: load_optional(args.results / f"{name}.json", notes)
              for name in ("baseline", "candidate", "gate")}
    deployment = load_optional(args.deployment_decision, notes) if args.deployment_decision else None
    model = build_model(**inputs, deployment=deployment,
                        preflight_status=os.getenv("PREFLIGHT_OUTCOME", ""), notes=notes)
    run_url = args.run_url
    if not run_url and os.getenv("GITHUB_REPOSITORY") and os.getenv("GITHUB_RUN_ID"):
        run_url = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    markdown = markdown_report(model, run_url)
    output = args.output or args.results
    output.mkdir(parents=True, exist_ok=True)
    (output / "evaluation-report.md").write_text(markdown, encoding="utf-8")
    (output / "evaluation-report.html").write_text(html_report(model, run_url), encoding="utf-8")
    if args.append_summary and os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(markdown)
    print(f"Rendered {model['state']} evaluation report: {output / 'evaluation-report.html'}")


if __name__ == "__main__":
    main()
