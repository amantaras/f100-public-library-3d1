import copy
import unittest

from check_readiness import check


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            "version": 1, "status": "ready_with_warnings",
            "checks": [
                {"name": name, "status": "ready"} for name in
                ("Latest eval gate", "Regression baseline", "Runtime monitoring")
            ] + [{"name": "Foundry continuous evaluation", "evidence": {"status": "ok"}}],
        }

    def test_complete_evidence_passes(self):
        check(self.evidence)

    def test_blocked_pack_fails_even_if_doctor_exits_zero(self):
        self.evidence["status"] = "blocked"
        with self.assertRaises(ValueError):
            check(self.evidence)

    def test_missing_sources_do_not_pass(self):
        for index in range(len(self.evidence["checks"])):
            with self.subTest(index=index):
                evidence = copy.deepcopy(self.evidence)
                evidence["checks"].pop(index)
                with self.assertRaises(ValueError):
                    check(evidence)
