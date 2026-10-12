import unittest

from agentops_evidence import convert
from test_quality_gate import report


class EvidenceTests(unittest.TestCase):
    def test_failed_gate_remains_failed_despite_high_average(self):
        output = convert(report(), {"passed": False, "failures": ["per-case regression"]})
        self.assertFalse(output["summary"]["overall_passed"])
        self.assertEqual(output["aggregate_metrics"]["relevance"], 5)
        self.assertEqual(output["config"]["engine"], "foundry-custom-invocations-adapter")

    def test_incomplete_report_cannot_become_evidence(self):
        fixture = report()
        fixture["output_items"].pop()
        with self.assertRaises(ValueError):
            convert(fixture, {"passed": True})
