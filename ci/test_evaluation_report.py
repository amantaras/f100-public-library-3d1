import copy
import json
from pathlib import Path
import tempfile
import unittest

from evaluation_report import (
    build_model, case_comparisons, html_report, load_optional, markdown_report, safe_url,
)
from quality_gate import compare
from test_quality_gate import report, set_not_applicable


class EvaluationReportTests(unittest.TestCase):
    def setUp(self):
        self.baseline = report()
        self.candidate = copy.deepcopy(self.baseline)
        self.baseline["provenance"]["version"] = "61"
        self.candidate["provenance"]["version"] = "62"
        self.baseline["run"]["report_url"] = "https://ai.azure.com/evaluations/baseline"
        self.candidate["run"]["report_url"] = "https://ai.azure.com/evaluations/candidate"

    def model(self):
        return build_model(self.baseline, self.candidate, compare(self.baseline, self.candidate))

    def test_not_applicable_tool_metrics_and_tool_traces_are_explicit(self):
        tool_metrics = ("tool_call_accuracy", "tool_selection", "tool_input_accuracy",
                        "tool_output_utilization", "tool_call_success")
        for case in range(14):
            self.baseline["output_items"][case]["datasource_item"]["tool_calls"] = []
            for name in tool_metrics:
                set_not_applicable(self.baseline, case, name)
        model = self.model()
        self.assertEqual(model["metrics"]["tool_call_accuracy"]["status"], "not_comparable")
        # Rows that expect no tool (policy_safety) are correct when nothing was called.
        self.assertEqual(model["metrics"]["tool_route_match"]["baseline_mean"], 2 / 14)
        self.assertTrue(model["cases"][0]["baseline_passed"] is False)
        summary = markdown_report(model)
        self.assertIn("Not comparable (N/A)", summary)
        self.assertIn("N/A (0 applicable) | 5.00 (unpaired, n=14)", summary)
        self.assertIn("`f100_public_library_3d1_fine_fees`", summary)
        document = html_report(model)
        self.assertIn("No tool call observed.", document)
        self.assertIn("<td>N/A</td>", document)
        self.assertIn('<h3 class="group">Tool use</h3>', document)
        self.assertIn("Indirect attack resistance", document)

    def test_improved_imperfect_scores_are_clear_not_hidden(self):
        for fixture in (self.baseline, self.candidate):
            fixture["output_items"][0]["results"][1].update(score=0, passed=False)
        self.baseline["output_items"][1]["results"][1].update(score=0, passed=False)
        model = self.model()
        self.assertEqual(model["state"], "passed")
        summary = markdown_report(model)
        self.assertIn("12/14", summary)
        self.assertIn("13/14", summary)
        self.assertIn("1 improved, 0 regressed, 13 unchanged", summary)
        self.assertIn("Existing failures remain visible", summary)
        self.assertIn("Foundry evaluation", summary)
        self.assertEqual(model["cases"][0]["change"], "unchanged")
        self.assertFalse(model["cases"][0]["candidate_passed"])
        self.assertEqual(model["cases"][1]["change"], "improved")

    def test_numeric_regression_is_visible_even_if_both_answers_pass(self):
        self.candidate["output_items"][0]["results"][0]["score"] = 4
        model = self.model()
        self.assertEqual(model["state"], "blocked")
        self.assertTrue(model["cases"][0]["candidate_passed"])
        self.assertEqual(model["cases"][0]["change"], "regressed")

    def test_forged_gate_cannot_render_as_approved(self):
        gate = compare(self.baseline, self.candidate)
        gate["metrics"]["relevance"]["candidate_mean"] = 4
        model = build_model(self.baseline, self.candidate, gate)
        self.assertEqual(model["state"], "error")
        self.assertEqual(model["cases"], [])

    def test_stale_or_incomplete_reports_do_not_render_scores(self):
        for mutate in (
            lambda r: r["output_items"].pop(),
            lambda r: r["provenance"].update(dataset_sha256="different"),
        ):
            candidate = copy.deepcopy(self.candidate)
            mutate(candidate)
            model = build_model(self.baseline, candidate, {"passed": True})
            self.assertEqual(model["state"], "error")
            self.assertEqual(model["metrics"], {})

    def test_auth_failure_is_not_called_a_quality_regression(self):
        self.candidate["run"]["status"] = "failed"
        self.candidate["failure_kind"] = "evaluation_authentication"
        model = build_model(self.baseline, self.candidate, None)
        self.assertEqual(model["state"], "error")
        self.assertIn("not an agent-quality verdict", model["message"])
        self.assertNotIn("14/14", markdown_report(model))

    def test_no_change_does_not_reuse_stale_scores_or_links(self):
        decision = {"status": "unchanged", "baseline_version": "62", "candidate_version": "62",
                    "evaluation_performed": False, "promotion_performed": False}
        model = build_model(self.baseline, self.candidate, compare(self.baseline, self.candidate), decision)
        self.assertEqual(model["state"], "unchanged")
        self.assertEqual(model["cases"], [])
        summary = markdown_report(model)
        self.assertNotIn("14/14", summary)
        self.assertNotIn("https://ai.azure.com/evaluations", summary)
        self.assertIn("No new agent evaluation or promotion", summary)
        self.assertIn("Verified no-change record", html_report(model))

    def test_equivalent_artifact_needs_explicit_proof(self):
        decision = {"status": "unchanged", "baseline_version": "68", "candidate_version": "69",
                    "equivalent_artifact": True, "evaluation_performed": False, "promotion_performed": False}
        model = build_model(None, None, None, decision)
        self.assertEqual(model["state"], "unchanged")
        self.assertIn("byte-identical to serving version 68", model["message"])
        self.assertEqual(build_model(None, None, None, {**decision, "equivalent_artifact": False})["state"], "error")

    def test_invalid_no_change_is_rejected(self):
        model = build_model(None, None, None, {"status": "unchanged"})
        self.assertEqual(model["state"], "error")

    def test_preflight_failure_and_missing_evidence_are_honest(self):
        self.assertEqual(build_model(None, None, None)["state"], "unavailable")
        model = build_model(None, None, None, preflight_status="failure")
        self.assertEqual(model["state"], "error")
        self.assertIn("before agent deployment", model["message"])

    def test_private_content_only_appears_escaped_in_html(self):
        private = 'PRIVATE_RESPONSE <script>alert("x")</script>'
        self.candidate["output_items"][0]["datasource_item"]["response"] = private
        self.candidate["output_items"][0]["results"][0]["reason"] = "PRIVATE_JUDGE <img src=x onerror=alert(1)>"
        model = self.model()
        summary = markdown_report(model)
        self.assertNotIn("PRIVATE_RESPONSE", summary)
        self.assertNotIn("PRIVATE_JUDGE", summary)
        document = html_report(model)
        self.assertIn("PRIVATE_RESPONSE", document)
        self.assertIn("&lt;script&gt;", document)
        self.assertNotIn('<script>alert("x")</script>', document)
        self.assertNotIn("<img src=x", document)
        self.assertIn("PRIVATE_JUDGE", document)

    def test_untrusted_urls_cannot_inject_html_markdown_or_credentials(self):
        for value in (
            "javascript:alert(1)", "https://evil.test/path", "https://ai.azure.com/?token=secret",
            'https://ai.azure.com/"><img>', "https://[bad",
            "https://ai.azure.com/ok)![image](https://evil.test/x)",
        ):
            with self.subTest(value=value):
                self.assertEqual(safe_url(value), "")
        self.assertEqual(safe_url("https://ai.azure.com/evaluations/1"), "https://ai.azure.com/evaluations/1")

    def test_case_matching_does_not_depend_on_api_row_order(self):
        self.candidate["output_items"].reverse()
        cases = case_comparisons(self.baseline, self.candidate)
        self.assertEqual([case["id"] for case in cases], [str(i) for i in range(14)])

    def test_malformed_json_is_reported_without_inventing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text("invalid-json")
            notes = []
            self.assertIsNone(load_optional(path, notes))
            self.assertIn("no result was inferred", notes[0])

    def test_theme_and_required_sections_are_present(self):
        document = html_report(self.model())
        for value in ("clawpilotTheme", "--cp-bg:", 'html[data-theme="dark"]',
                      "Segoe UI", "Case-by-case evidence", "Execution and provenance"):
            self.assertIn(value, document)
        self.assertEqual(document.count('<details class="case"'), 14)


if __name__ == "__main__":
    unittest.main()
