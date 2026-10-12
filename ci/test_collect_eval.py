import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from collect_eval import collect, EvaluationInfrastructureError


class CollectionTests(unittest.TestCase):
    @patch("collect_eval.get_credential")
    @patch("collect_eval.AIProjectClient")
    def test_failed_cloud_run_retains_provenance(self, project_type, credential):
        project = project_type.return_value.__enter__.return_value
        client = project.get_openai_client.return_value.__enter__.return_value
        run = MagicMock()
        run.status = "failed"
        run.model_dump.return_value = {"id": "run", "eval_id": "eval", "status": "failed"}
        client.evals.runs.retrieve.return_value = run
        client.evals.retrieve.return_value.model_dump.return_value = {"id": "eval"}
        client.evals.runs.output_items.list.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with self.assertRaisesRegex(RuntimeError, "Evaluation failed"):
                collect("https://example.test", "eval", "run", output, 1,
                        provenance={"artifact": "immutable"})
            self.assertEqual(json.loads(output.read_text())["provenance"]["artifact"], "immutable")

    @patch("collect_eval.time.sleep")
    @patch("collect_eval.get_credential")
    @patch("collect_eval.AIProjectClient")
    def test_waits_for_all_rows_after_completed_status(self, project_type, credential, sleep):
        client = project_type.return_value.__enter__.return_value.get_openai_client.return_value.__enter__.return_value
        run = MagicMock()
        run.status = "completed"
        run.model_dump.return_value = {"id": "run", "eval_id": "eval", "status": "completed"}
        client.evals.runs.retrieve.return_value = run
        client.evals.retrieve.return_value.model_dump.return_value = {"id": "eval"}
        first, second = MagicMock(), MagicMock()
        first.model_dump.return_value = {"id": "1"}
        second.model_dump.return_value = {"id": "2"}
        client.evals.runs.output_items.list.side_effect = [[first], [first, second]]
        with tempfile.TemporaryDirectory() as directory:
            result = collect("https://example.test", "eval", "run", Path(directory) / "result.json",
                             120, expected_items=2)
        self.assertEqual(len(result["output_items"]), 2)
        sleep.assert_called_once_with(5)

    @patch("collect_eval.time.monotonic", side_effect=[0, 0, 61])
    @patch("collect_eval.get_credential")
    @patch("collect_eval.AIProjectClient")
    def test_incomplete_terminal_results_fail_closed(self, project_type, credential, clock):
        client = project_type.return_value.__enter__.return_value.get_openai_client.return_value.__enter__.return_value
        run = MagicMock()
        run.status = "completed"
        run.model_dump.return_value = {"status": "completed"}
        client.evals.runs.retrieve.return_value = run
        client.evals.retrieve.return_value.model_dump.return_value = {"id": "eval"}
        client.evals.runs.output_items.list.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with self.assertRaises(EvaluationInfrastructureError):
                collect("https://example.test", "eval", "run", output, 120, expected_items=2)
            self.assertEqual(json.loads(output.read_text())["failure_kind"], "incomplete_evaluation")
