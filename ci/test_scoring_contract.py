import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from collect_eval import normalize_grades
from quality_gate import CRITERIA, DATASET, EVALUATOR, FOUNDRY_METRICS, RUBRIC_EVALUATOR
from register_evaluator import verify_evaluator
from scoring_preflight import CONTROL_METRICS, controls, validate_controls

CONTROLS = {name: FOUNDRY_METRICS[name] for name in CONTROL_METRICS}


def native_item(case="positive", score=1, relevance=4):
    return {
        "datasource_item": {"case_id": case},
        "results": [
            {"name": "relevance", "score": relevance, "passed": relevance >= 3, "status": "completed"},
            {"name": "expected_behavior", "score": score, "passed": bool(score), "status": "completed"},
        ],
    }


class ScoringContractTests(unittest.TestCase):
    def test_release_uses_native_pinned_evaluator(self):
        criteria = json.loads(CRITERIA.read_text())
        self.assertTrue(all(c["type"] == "azure_ai_evaluator" for c in criteria))
        behavior = next(c for c in criteria if c["name"] == "expected_behavior")
        self.assertEqual(behavior["evaluator_name"], json.loads(EVALUATOR.read_text())["name"])
        self.assertEqual(behavior["evaluator_version"], "1")

    def test_native_single_and_dual_grade_shapes(self):
        single = native_item()
        self.assertEqual(normalize_grades([single], CONTROLS)[0]["results"],
                         [{**grade, "applicable": True} for grade in single["results"]])
        dual = copy.deepcopy(single)
        dual["results"][1]["passed"] = None
        dual["results"].append({"name": "expected_behavior", "score": None, "passed": True})
        result = normalize_grades([dual], CONTROLS)[0]
        self.assertEqual(result["results"][1]["score"], 1)
        self.assertTrue(result["results"][1]["passed"])
        self.assertEqual(len(result["raw_results"]), 3)

    def test_ambiguous_missing_or_invalid_grades_are_not_defaulted(self):
        for mutate in (
            lambda item: item["results"].pop(),
            lambda item: item["results"].append(dict(item["results"][1])),
            lambda item: item["results"][1].update(score=None),
            lambda item: item["results"][1].update(score=0.5),
            lambda item: item["results"][1].update(passed=False),
            lambda item: item["results"][1].update(score=float("nan")),
            lambda item: item["results"][1].update(error={"message": "failed"}),
        ):
            item = native_item()
            mutate(item)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                normalize_grades([item], CONTROLS)

    def test_positive_negative_injection_and_medication_controls(self):
        report = {"output_items": [
            native_item("positive"), native_item("negative", 0, 1),
            native_item("injection", 0, 1), native_item("domain_control"),
        ]}
        validate_controls(report)
        report["output_items"][1]["results"][1].update(score=1, passed=True)
        with self.assertRaises(ValueError):
            validate_controls(report)

    def test_reference_accepts_conservative_guidance_without_dosing_regimen(self):
        rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
        reference = rows[7]
        self.assertEqual(len(rows), 14)
        control = controls()[-1]["item"]
        self.assertEqual(control["expected_behavior"], reference["expected_behavior"])

    def test_full_agentic_suite_is_configured_natively(self):
        criteria = json.loads(CRITERIA.read_text())
        self.assertEqual({c["name"] for c in criteria}, set(FOUNDRY_METRICS))
        rubric = next(c for c in criteria if c["name"] == "domain_rubric")
        self.assertEqual(rubric["evaluator_name"], json.loads(RUBRIC_EVALUATOR.read_text())["name"])
        for name in ("tool_call_accuracy", "tool_selection", "tool_input_accuracy"):
            mapping = next(c for c in criteria if c["name"] == name)["data_mapping"]
            self.assertEqual(mapping["tool_definitions"], "{{item.tool_definitions}}")
        self.assertEqual(next(c for c in criteria if c["name"] == "tool_call_accuracy")["data_mapping"]["tool_calls"],
                         "{{item.tool_calls}}")

    def test_not_applicable_only_for_optional_metrics(self):
        skipped = {"score": None, "passed": None, "status": "skipped", "label": "not_applicable",
                   "reason": "Not applicable: No tool calls found."}
        item = {"datasource_item": {"case_id": "0"}, "results": [
            {"name": name, "score": 1, "passed": True, "status": "completed"} for name in FOUNDRY_METRICS]}
        for grade in item["results"]:
            if grade["name"] in ("relevance", "intent_resolution", "tool_call_accuracy"):
                grade["score"] = 5
        optional = copy.deepcopy(item)
        next(g for g in optional["results"] if g["name"] == "tool_call_accuracy").update(skipped)
        graded = {g["name"]: g for g in normalize_grades([optional])[0]["results"]}
        self.assertFalse(graded["tool_call_accuracy"]["applicable"])
        self.assertIsNone(graded["tool_call_accuracy"]["score"])
        required = copy.deepcopy(item)
        next(g for g in required["results"] if g["name"] == "task_adherence").update(skipped)
        with self.assertRaises(ValueError):
            normalize_grades([required])

    def test_indirect_injection_cases_are_in_the_release_suite(self):
        rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
        injections = [row for row in rows if row["category"] == "indirect_prompt_injection"]
        self.assertEqual(len(injections), 3)
        self.assertTrue(all(row["tool_calls"] for row in rows if row["category"] != "policy_safety"))
        self.assertTrue(all(not row["tool_calls"] and not row["tool_definitions"]
                            for row in rows if row["category"] == "policy_safety"))

    def test_remote_evaluator_drift_is_rejected(self):
        spec = json.loads(EVALUATOR.read_text())
        project = Mock()
        project.beta.evaluators.get_version.return_value.as_dict.return_value = {
            "definition": copy.deepcopy(spec["definition"])
        }
        verify_evaluator(project, spec["name"], "1")
        project.beta.evaluators.get_version.return_value.as_dict.return_value["definition"]["prompt_text"] = "different"
        with self.assertRaises(ValueError):
            verify_evaluator(project, spec["name"], "1")
