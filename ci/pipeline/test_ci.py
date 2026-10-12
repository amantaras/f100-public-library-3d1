import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_dataset  # noqa: E402
import redteam_summary  # noqa: E402
from agentops_eval import (  # noqa: E402
    CLOUD_TIMEOUT_MINUTES, all_output_items, cloud_tool_mapping, cloud_wait_minutes, parallel_rows, patient_cloud_runs,
    tool_calls_for_evaluators,
)
from regression_gate import compare  # noqa: E402


def results(**metrics):
    return {"aggregate_metrics": metrics, "summary": {}}


class RegressionGateTests(unittest.TestCase):
    def test_judge_noise_within_half_a_point_passes(self):
        rows = compare(results(coherence=4.6, task_adherence=0.9), results(coherence=4.2, task_adherence=0.88))
        self.assertFalse(any(r["regressed"] for r in rows))

    def test_real_drops_block(self):
        rows = {r["metric"]: r for r in compare(results(response_completeness=4.4, task_adherence=0.95),
                                                  results(response_completeness=3.1, task_adherence=0.7))}
        self.assertTrue(rows["response_completeness"]["regressed"])
        self.assertTrue(rows["task_adherence"]["regressed"])

    def test_one_flipped_case_on_a_small_dataset_is_noise_not_a_regression(self):
        small = {"aggregate_metrics": {"task_adherence": 0.75}, "summary": {"items_total": 12}}
        flipped = {"aggregate_metrics": {"task_adherence": 0.667}, "summary": {"items_total": 12}}  # one case of 12
        self.assertFalse(compare(small, flipped)[0]["regressed"])
        three_down = {"aggregate_metrics": {"task_adherence": 0.5}, "summary": {"items_total": 12}}  # three cases
        self.assertTrue(compare(small, three_down)[0]["regressed"])

    def test_latency_only_blocks_when_it_doubles(self):
        self.assertFalse(compare(results(avg_latency_seconds=20), results(avg_latency_seconds=28))[0]["regressed"])
        self.assertTrue(compare(results(avg_latency_seconds=20), results(avg_latency_seconds=45))[0]["regressed"])


class DatasetTests(unittest.TestCase):
    def test_committed_agentops_dataset_matches_the_seed(self):
        root = Path(__file__).resolve().parents[2]
        rows = [json.loads(l) for l in (root / "evaluations/agentops-dataset.jsonl").read_text().splitlines()]
        seed = [json.loads(l) for l in (root / "evaluations/public-library-3d1-seed-v1.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), len(seed))
        self.assertEqual(rows[0]["input"], seed[0]["query"])
        self.assertIsInstance(rows[0]["tool_definitions"], list)


class CloudWaitTests(unittest.TestCase):
    def test_a_cloud_run_waits_long_enough_for_a_full_dataset_and_polls_less_often(self):
        seen = {}

        def run(*, dataset_path, poll_interval_seconds=2.0, max_poll_attempts=300):
            seen.update(interval=poll_interval_seconds, attempts=max_poll_attempts, dataset=dataset_path)
            return "result"

        self.assertEqual(patient_cloud_runs(run, CLOUD_TIMEOUT_MINUTES)(dataset_path="d.jsonl"), "result")
        self.assertEqual(seen, {"interval": 10.0, "attempts": 180, "dataset": "d.jsonl"})  # 30 minutes, not AgentOps' 10
        patient_cloud_runs(run, 30)(dataset_path="d.jsonl", poll_interval_seconds=5.0)
        self.assertEqual((seen["interval"], seen["attempts"]), (5.0, 360))
        patient_cloud_runs(run, 30)(dataset_path="d.jsonl", max_poll_attempts=7)
        self.assertEqual(seen["attempts"], 7, "an explicit caller choice wins")

    def test_the_wait_can_be_tuned_and_stays_inside_the_45_minute_release_jobs(self):
        self.assertLess(CLOUD_TIMEOUT_MINUTES, 45)
        self.assertEqual(cloud_wait_minutes({}), CLOUD_TIMEOUT_MINUTES)
        self.assertEqual(cloud_wait_minutes({"AGENTOPS_CLOUD_TIMEOUT_MINUTES": "20"}), 20.0)
        for bad in ("soon", "0", "-5"):
            with self.assertRaises(SystemExit):
                cloud_wait_minutes({"AGENTOPS_CLOUD_TIMEOUT_MINUTES": bad})


class ParallelRowsTests(unittest.TestCase):
    def test_rows_are_scored_concurrently_in_order(self):
        rows = [{"input": str(i)} for i in range(6)]
        active, peak, lock = [0], [0], threading.Lock()

        def evaluate_row(*, row, index, total, progress):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return index

        patched = parallel_rows(evaluate_row, rows, workers=3)
        self.assertEqual([patched(row=r, index=i, total=6, progress=print) for i, r in enumerate(rows)], list(range(6)))
        self.assertEqual(peak[0], 3)


class ToolCallAdapterTests(unittest.TestCase):
    def test_responses_function_calls_become_evaluator_tool_calls(self):
        calls = tool_calls_for_evaluators([{"type": "function_call", "call_id": "c1", "name": "example-tool",
                                            "arguments": "{\"query\": \"q\"}"}, {"type": "tool_call", "name": "x"}])
        self.assertEqual(calls[0], {"type": "tool_call", "tool_call_id": "c1", "name": "example-tool",
                                    "arguments": {"query": "q"}})
        self.assertEqual(calls[1], {"type": "tool_call", "name": "x"})
        self.assertIsNone(tool_calls_for_evaluators(None))

    def test_cloud_tool_accuracy_reads_the_agent_output_items(self):
        class Preset:
            class_name = "ToolCallAccuracyEvaluator"
        mapping = cloud_tool_mapping(lambda preset: {"query": "{{item.input}}"})(Preset())
        self.assertEqual(mapping["response"], "{{sample.output_items}}")
        self.assertNotIn("tool_calls", mapping)


class OutputItemsTests(unittest.TestCase):
    def test_every_page_is_downloaded(self):
        class Pages:  # the SDK paginator iterates across pages; .data holds only the first
            data = [{"id": 0}]
            def __iter__(self):
                return iter([{"id": i} for i in range(26)])
        class Api:
            def list(self, **kwargs):
                self.kwargs = kwargs
                return Pages()
        api = Api()
        client = type("C", (), {"evals": type("E", (), {"runs": type("R", (), {"output_items": api})()})()})()
        items = all_output_items(lambda raw: raw)(client, eval_id="e", run_id="r", progress=lambda m: None)
        self.assertEqual(len(items), 26)
        self.assertEqual(api.kwargs["limit"], 100)


class RedTeamSummaryTests(unittest.TestCase):
    def test_counts_only(self):
        text = redteam_summary.render({"successful_attacks": 0, "total_attempts": 8, "attack_success_rate": 0.0,
                                       "fail_threshold": 0.2, "per_category": {"self_harm": {"successful": 0, "total": 2}},
                                       "attack_strategies": ["base64"]})
        self.assertIn("0 of 8 attacks", text)
        self.assertIn("self harm", text)


if __name__ == "__main__":
    unittest.main()
