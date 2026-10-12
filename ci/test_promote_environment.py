import copy
import unittest
from unittest.mock import Mock, patch

from azure.core.exceptions import ResourceNotFoundError

import promote_environment as pe
from test_quality_gate import report

REVISION = "a" * 40
TOOLBOX = {"toolbox": "f100-public-library-3d1-toolbox:1", "toolbox_sha256": "t" * 64, "toolbox_resources": []}


def dev_evidence():
    baseline, candidate = report(), report()
    baseline["provenance"]["version"] = "66"
    candidate["provenance"].update(version="67", artifact="hash-67", evaluation_git_sha=REVISION,
                                   deployed_git_sha=REVISION, toolbox="f100-public-library-3d1-toolbox:1",
                                   toolbox_sha256=TOOLBOX["toolbox_sha256"])
    deployment = {"status": "promoted", "smoke_passed": True, "selected_version": "67",
                  "candidate_provenance": copy.deepcopy(candidate["provenance"])}
    return deployment, baseline, candidate


class DevEvidenceTests(unittest.TestCase):
    def verify(self, deployment, baseline, candidate, gate=None):
        gate = gate if gate is not None else {"passed": True, "metrics": {"relevance": {}}}
        return pe.verify_dev_evidence(deployment, baseline, candidate, gate)

    def test_promoted_passing_evidence_is_accepted(self):
        self.assertEqual(self.verify(*dev_evidence())["artifact"], "hash-67")

    def test_unpromoted_mismatched_or_incomplete_evidence_is_rejected(self):
        for mutate in (
            lambda d, b, c: d.update(status="blocked"),
            lambda d, b, c: d.update(smoke_passed=False),
            lambda d, b, c: d.update(selected_version="66"),
            lambda d, b, c: d["candidate_provenance"].update(artifact="other"),
            lambda d, b, c: c["provenance"].pop("toolbox_sha256") or d["candidate_provenance"].pop("toolbox_sha256"),
            lambda d, b, c: c["output_items"][0]["results"][0].update(score=4),
        ):
            deployment, baseline, candidate = dev_evidence()
            mutate(deployment, baseline, candidate)
            with self.subTest(mutate=mutate), self.assertRaises((ValueError, KeyError)):
                self.verify(deployment, baseline, candidate)

    def test_recorded_gate_must_pass(self):
        with self.assertRaises(ValueError):
            self.verify(*dev_evidence(), gate={"passed": False, "metrics": {"relevance": {}}})

    def test_changed_contract_relies_on_the_recorded_passing_gate(self):
        deployment, baseline, candidate = dev_evidence()
        for report in (baseline, candidate):
            report["provenance"]["policy_sha256"] = "older-policy"
        deployment["candidate_provenance"] = candidate["provenance"]
        self.assertEqual(self.verify(deployment, baseline, candidate)["version"], "67")


@patch("promote_environment.source_revision", return_value=REVISION)
@patch("promote_environment.toolbox_reference", return_value=("f100-public-library-3d1-toolbox", "1"))
@patch("promote_environment.toolbox_fingerprint", return_value=TOOLBOX)
@patch("promote_environment.current_route", return_value="3")
class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.evaluated = dev_evidence()[2]["provenance"]
        self.identity = {"artifact": "hash-67", "deployed_git_sha": REVISION, "definition_sha256": "d"}

    def verify(self, identity=None):
        with patch("promote_environment.artifact_identity", return_value=identity or self.identity):
            return pe.verify_candidate(Mock(), "https://prod.test", "token", "pilot", "4", "3", self.evaluated)

    def test_same_artifact_and_toolbox_is_a_candidate(self, *_):
        record = self.verify()
        self.assertEqual(record["status"], "candidate")
        self.assertEqual(record["evaluated_in"]["version"], "67")

    def test_different_artifact_revision_or_toolbox_blocks(self, _route, fingerprint, _ref, _rev):
        with self.assertRaisesRegex(ValueError, "artifact"):
            self.verify({**self.identity, "artifact": "other"})
        with self.assertRaisesRegex(ValueError, "checked-out revision"):
            self.verify({**self.identity, "deployed_git_sha": "b" * 40})
        fingerprint.return_value = {**TOOLBOX, "toolbox_sha256": "x" * 64}
        with self.assertRaisesRegex(ValueError, "toolbox content"):
            self.verify()

    def test_bootstrap_requires_the_candidate_to_be_the_newest_version(self, *_):
        project = Mock()
        project.agents.list_versions.return_value = [Mock(as_dict=Mock(return_value={"version": v})) for v in ("1", "2")]
        with patch("promote_environment.artifact_identity", return_value=self.identity):
            record = pe.verify_candidate(project, "https://prod.test", "t", "pilot", "2", pe.NO_VERSION, self.evaluated)
            self.assertEqual(record["status"], "candidate")
            with self.assertRaisesRegex(ValueError, "newest production version"):
                pe.verify_candidate(project, "https://prod.test", "t", "pilot", "1", pe.NO_VERSION, self.evaluated)

    def test_route_change_during_deploy_blocks(self, route, *_):
        route.return_value = "2"
        with self.assertRaisesRegex(ValueError, "routing changed"):
            self.verify()


class PromotionTests(unittest.TestCase):
    @patch("promote_environment.smoke", side_effect=RuntimeError("no triage call"))
    @patch("promote_environment.route", return_value="4")
    @patch("promote_environment.pin")
    def test_smoke_failure_rolls_back_to_previous(self, pin, _route, _smoke):
        with self.assertRaises(RuntimeError):
            pe.promote(Mock(), Mock(), "https://prod.test", "pilot", "4", "3")
        self.assertEqual([call.args[2] for call in pin.call_args_list], ["4", "3"])

    @patch("promote_environment.smoke", side_effect=RuntimeError("no triage call"))
    @patch("promote_environment.route", return_value="1")
    @patch("promote_environment.pin")
    def test_bootstrap_failure_is_not_left_released(self, pin, _route, _smoke):
        project = Mock()
        with self.assertRaises(RuntimeError):
            pe.promote(project, Mock(), "https://prod.test", "pilot", "1", pe.NO_VERSION)
        pin.assert_called_once()
        body = project.beta.agents.patch_agent_details.call_args.kwargs["body"]
        rule = body["agent_endpoint"]["version_selector"]["version_selection_rules"][0]
        self.assertEqual(rule["agent_version"], "@latest")

    @patch("promote_environment.invoke")
    def test_routing_smoke_requires_an_observed_triage_call(self, invoke):
        invoke.side_effect = [
            {"text": "Bring records.", "tool_calls": [], "tool_definitions": None},
            {"text": "Go now.", "tool_calls": [], "tool_definitions": [{"name": "f100_public_library_3d1_fine_fees"}]},
        ]
        with self.assertRaisesRegex(RuntimeError, "f100_public_library_3d1_fine_fees"):
            pe.smoke(Mock(), Mock(), "https://prod.test", "pilot", "4")

    def test_missing_agent_is_a_bootstrap(self):
        project = Mock()
        project.agents.get.side_effect = ResourceNotFoundError("missing")
        self.assertEqual(pe.current_route(project, "pilot"), pe.NO_VERSION)

    def test_unpinned_azd_default_is_not_a_release(self):
        project = Mock()
        project.agents.get.return_value.as_dict.return_value = {"agent_endpoint": {"version_selector": {
            "version_selection_rules": [{"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}]}}}
        self.assertEqual(pe.current_route(project, "pilot"), pe.NO_VERSION)


if __name__ == "__main__":
    unittest.main()
