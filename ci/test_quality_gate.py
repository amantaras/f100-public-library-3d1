import copy
import hashlib
import json
import unittest

from types import SimpleNamespace

from evaluate import parse_stream, parse_stream_events, policy_refusal
from quality_gate import (CRITERIA, DATASET, FOUNDRY_METRICS, INSTRUCTIONS, compare, evaluator_sha256,
                          __file__ as policy_file)
from pathlib import Path

SCORES = {"relevance": 5, "intent_resolution": 5, "tool_call_accuracy": 5}


def result(name: str) -> dict:
    return {"name": name, "score": SCORES.get(name, 1), "passed": True, "applicable": True}


def report():
    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    return {
        "provenance": {
            "agent": "pilot", "endpoint": "https://example.test", "version": "52",
            "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
            "criteria_sha256": hashlib.sha256(CRITERIA.read_bytes()).hexdigest(),
            "evaluator_sha256": evaluator_sha256(),
            "instructions_sha256": hashlib.sha256(INSTRUCTIONS.read_bytes()).hexdigest(),
            "policy_sha256": hashlib.sha256(Path(policy_file).read_bytes()).hexdigest(),
            "case_count": len(rows),
        },
        "run": {"status": "completed", "id": "run-test", "eval_id": "eval-test", "created_at": 1790320000},
        "output_items": [
            {
                "status": "completed",
                "eval_id": "eval-test", "run_id": "run-test",
                "datasource_item": {
                    "case_id": str(index), "query": row["query"],
                    "expected_behavior": row["expected_behavior"], "response": "Test fixture",
                    "tool_calls": observed_tool_calls(row), "tool_definitions": row["tool_definitions"],
                    "tool_definitions_source": "runtime",
                },
                "results": [result(name) for name in FOUNDRY_METRICS],
            }
            for index, row in enumerate(rows)
        ],
    }


def set_not_applicable(fixture: dict, case: int, name: str):
    grade = next(r for r in fixture["output_items"][case]["results"] if r["name"] == name)
    grade.update(score=None, passed=None, applicable=False, status="skipped", label="not_applicable")


def grade(fixture: dict, case: int, name: str) -> dict:
    return next(r for r in fixture["output_items"][case]["results"] if r["name"] == name)


def observed_tool_calls(row: dict) -> list[dict]:
    """Model real evaluation output; expected dataset calls need not carry runtime event metadata."""
    return [
        {
            "type": "tool_call",
            "tool_call_id": call.get("tool_call_id") or f"fixture_call_{index}",
            "name": call["name"],
            "arguments": call.get("arguments", {}),
        }
        for index, call in enumerate(row.get("tool_calls") or [])
    ]


class GateTests(unittest.TestCase):
    def setUp(self):
        self.baseline = report()
        self.candidate = copy.deepcopy(self.baseline)

    def test_equal_quality_passes(self):
        self.assertTrue(compare(self.baseline, self.candidate)["passed"])

    def test_lower_score_blocks_even_when_grader_passed(self):
        self.candidate["output_items"][0]["results"][0]["score"] = 4
        self.assertFalse(compare(self.baseline, self.candidate)["passed"])

    def test_behavior_failure_blocks(self):
        self.candidate["output_items"][0]["results"][1].update(score=0, passed=False)
        self.assertFalse(compare(self.baseline, self.candidate)["passed"])

    def test_improvement_does_not_hide_per_case_regression(self):
        self.baseline["output_items"][1]["results"][0]["score"] = 4
        self.candidate["output_items"][0]["results"][0]["score"] = 4
        self.assertFalse(compare(self.baseline, self.candidate)["passed"])

    def test_no_unrequested_absolute_floor_for_equal_scores(self):
        for fixture in (self.baseline, self.candidate):
            for item in fixture["output_items"]:
                item["results"][0]["score"] = 3
        self.assertTrue(compare(self.baseline, self.candidate)["passed"])

    def test_equal_imperfect_behavior_passes_non_regression(self):
        for fixture in (self.baseline, self.candidate):
            fixture["output_items"][0]["results"][1].update(score=0, passed=False)
        result = compare(self.baseline, self.candidate)
        self.assertTrue(result["passed"])
        self.assertEqual(result["candidate_failed_checks"], 1)

    def test_improvement_passes_without_perfect_score(self):
        for fixture in (self.baseline, self.candidate):
            fixture["output_items"][0]["results"][1].update(score=0, passed=False)
        self.baseline["output_items"][1]["results"][1].update(score=0, passed=False)
        result = compare(self.baseline, self.candidate)
        self.assertTrue(result["passed"])
        self.assertEqual(result["policy"], "baseline_non_regression")

    def test_fail_closed(self):
        for mutation in (
            lambda r: r["output_items"].pop(),
            lambda r: r["run"].update(status="failed"),
            lambda r: r["provenance"].update(dataset_sha256="changed"),
            lambda r: r["output_items"][0]["results"].pop(),
            lambda r: r["output_items"][0]["results"][0].update(passed=None),
            lambda r: r["output_items"][0]["results"][0].update(score=float("nan")),
            lambda r: r["output_items"][0]["results"][0].update(score=True),
            lambda r: r["output_items"][0]["datasource_item"].update(response=""),
            lambda r: r["output_items"][0]["datasource_item"].update(query="different"),
            lambda r: r["output_items"][0].update(sample={"error": {"message": "failure"}}),
            lambda r: r["output_items"][1]["datasource_item"].update(case_id="0"),
            lambda r: r["output_items"][0]["results"][1].update(name="relevance"),
            lambda r: r["output_items"][0]["datasource_item"].update(tool_calls=None),
            lambda r: r["output_items"][0]["datasource_item"].update(tool_definitions_source="invented"),
            lambda r: r["output_items"][0]["datasource_item"]["tool_calls"].append({"name": "x"}),
            lambda r: r["output_items"][0].update(run_id="wrong-run"),
            lambda r: r["output_items"][0]["results"][0].update(status="error"),
        ):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(self.candidate)
                mutation(candidate)
                with self.assertRaises((ValueError, KeyError)):
                    compare(self.baseline, candidate)

    def test_aggregate_metric_tolerates_one_case_of_judge_disagreement(self):
        grade(self.candidate, 0, "task_adherence").update(score=0, passed=False)
        result = compare(self.baseline, self.candidate)
        self.assertTrue(result["passed"])
        self.assertIn("case 0 task_adherence: regression", result["watch"])

    def test_aggregate_metric_blocks_beyond_one_case(self):
        for case in (0, 1):
            grade(self.candidate, case, "task_completion").update(score=0, passed=False)
        result = compare(self.baseline, self.candidate)
        self.assertFalse(result["passed"])
        self.assertIn("task_completion: aggregate regression", result["failures"])

    def test_indirect_attack_is_strict_per_case(self):
        grade(self.candidate, 8, "indirect_attack").update(score=0, passed=False)
        result = compare(self.baseline, self.candidate)
        self.assertFalse(result["passed"])
        self.assertIn("case 8 indirect_attack: regression", result["failures"])

    def test_tool_metrics_compare_only_paired_applicable_cases(self):
        for case in range(14):
            set_not_applicable(self.baseline, case, "tool_call_accuracy")
        result = compare(self.baseline, self.candidate)
        self.assertTrue(result["passed"])
        self.assertEqual(result["metrics"]["tool_call_accuracy"]["status"], "not_comparable")

    def test_lost_tool_route_is_measured_deterministically(self):
        for case in (0, 1):
            self.candidate["output_items"][case]["datasource_item"]["tool_calls"] = []
            for name in ("tool_call_accuracy", "tool_selection", "tool_input_accuracy",
                         "tool_output_utilization", "tool_call_success"):
                set_not_applicable(self.candidate, case, name)
        result = compare(self.baseline, self.candidate)
        self.assertEqual(result["metrics"]["tool_route_match"]["candidate_mean"], 12 / 14)
        self.assertIn("tool_route_match: aggregate regression", result["failures"])

    def test_required_metric_cannot_be_not_applicable(self):
        set_not_applicable(self.candidate, 0, "task_adherence")
        with self.assertRaises(ValueError):
            compare(self.baseline, self.candidate)

    def test_instruction_change_is_incomparable(self):
        self.candidate["provenance"]["instructions_sha256"] = "changed"
        with self.assertRaises(ValueError):
            compare(self.baseline, self.candidate)


class StreamTests(unittest.TestCase):
    def test_complete_stream(self):
        self.assertEqual(parse_stream('data: {"delta":"Hello"}\n\nevent: done\ndata: {}\n\n'), "Hello")

    def test_crlf_stream(self):
        self.assertEqual(parse_stream('data: {"delta":"Hi"}\r\n\r\nevent: done\r\ndata: {}\r\n\r\n'), "Hi")

    def test_error_even_with_done_is_failure(self):
        with self.assertRaises(RuntimeError):
            parse_stream('data: {"type":"error","message":"failed"}\n\nevent: done\ndata: {}\n\n')

    def test_a_prompt_refused_by_the_model_service_is_a_refusal_to_grade(self):
        stream = ('data: {"type":"error","message":"{\\"code\\":\\"cyber_policy\\",\\"message\\":\\"flagged\\"}"}\n\n'
                  'event: done\ndata: {}\n\n')
        self.assertIn("refused this prompt", parse_stream(stream))

    def test_a_policy_block_after_a_real_answer_keeps_the_answer(self):
        stream = ('data: {"delta":"I cannot help."}\n\ndata: {"type":"error","message":"content_filter"}\n\n'
                  'event: done\ndata: {}\n\n')
        self.assertEqual(parse_stream(stream), "I cannot help.")

    def test_an_input_stage_content_safety_block_is_a_refusal_not_a_failed_collection(self):
        body = '{"error": {"code": "content_filter", "message": "blocked at input stage"}}'
        refusal = policy_refusal(SimpleNamespace(status_code=400, text=body))
        self.assertTrue(refusal["text"] and refusal["tool_calls"] == [])
        self.assertIsNone(policy_refusal(SimpleNamespace(status_code=400, text='{"error": "bad payload"}')))
        self.assertIsNone(policy_refusal(SimpleNamespace(status_code=500, text=body)))

    def test_empty_truncated_and_invalid_streams(self):
        for stream in ("", 'data: {"delta":"partial"}\n\n', 'event: done\ndata: {}\n\n', "data: not-json\n\n"):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                parse_stream(stream)

    def test_tool_events_are_captured(self):
        stream = (
            'data: {"type":"tool_definitions","tools":[{"name":"f100_public_library_3d1_fine_fees"}]}\n\n'
            'data: {"type":"tool_call","tool_call_id":"c1","name":"f100_public_library_3d1_fine_fees","arguments":{"query":"x"}}\n\n'
            'data: {"type":"tool_result","tool_call_id":"c1","success":true,"result":"guidance"}\n\n'
            'data: {"delta":"Go now."}\n\nevent: done\ndata: {}\n\n'
        )
        observed = parse_stream_events(stream)
        self.assertEqual(observed["text"], "Go now.")
        self.assertEqual(observed["tool_calls"], [{"type": "tool_call", "tool_call_id": "c1",
                                                   "name": "f100_public_library_3d1_fine_fees", "arguments": {"query": "x"}}])
        self.assertEqual(observed["tool_results"]["c1"]["result"], "guidance")
        self.assertEqual(observed["tool_definitions"], [{"name": "f100_public_library_3d1_fine_fees"}])

    def test_orphan_tool_result_and_post_done_frames_fail(self):
        for stream in (
            'data: {"type":"tool_result","tool_call_id":"c9","success":true}\n\ndata: {"delta":"x"}\n\nevent: done\ndata: {}\n\n',
            'data: {"delta":"x"}\n\nevent: done\ndata: {}\n\ndata: {"type":"tool_call","tool_call_id":"c1","name":"t"}\n\n',
        ):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                parse_stream_events(stream)


if __name__ == "__main__":
    unittest.main()
