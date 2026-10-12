import unittest

from continuous_evaluation import EVALUATORS, criteria, markdown, summarize


class ContinuousEvaluationTests(unittest.TestCase):
    def test_criteria_are_native_and_safety_needs_no_judge_model(self):
        built = {c["name"]: c for c in criteria("gpt-6-luna")}
        self.assertEqual(set(built), set(EVALUATORS))
        self.assertTrue(all(c["type"] == "azure_ai_evaluator" for c in built.values()))
        self.assertEqual(built["indirect_attack"]["initialization_parameters"], {})
        self.assertEqual(built["relevance"]["initialization_parameters"], {"deployment_name": "gpt-6-luna"})

    def test_summary_keeps_not_applicable_separate(self):
        items = [{"results": [{"name": "relevance", "score": 5, "passed": True}]},
                 {"results": [{"name": "relevance", "score": 3, "passed": False},
                              {"name": "task_adherence", "score": None, "passed": None}]}]
        summary = summarize(items)
        self.assertEqual(summary["relevance"], {"mean": 4, "passed": 1, "failed": 1, "not_applicable": 0})
        self.assertIsNone(summary["task_adherence"]["mean"])
        self.assertEqual(summary["task_adherence"]["not_applicable"], 1)

    def test_no_traces_is_reported_not_scored(self):
        text = markdown({"status": "no_traces", "window_hours": 24})
        self.assertIn("nothing was scored", text)


if __name__ == "__main__":
    unittest.main()
