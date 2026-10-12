import copy
from unittest.mock import Mock, patch
import unittest

from release import promote, route, selection, smoke_or_rollback, inspect_candidate
from test_quality_gate import report


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.project = Mock()
        self.project.agents.get.return_value.as_dict.return_value = {
            "agent_endpoint": {"version_selector": selection("52")}
        }
        self.baseline = report()
        self.candidate = copy.deepcopy(self.baseline)
        self.candidate["provenance"]["version"] = "53"

    def test_no_latest_routing(self):
        for version in ("@latest", "", "-1", "1\n", "1;echo"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                selection(version)

    def test_read_fixed_route(self):
        self.assertEqual(route(self.project, "pilot"), "52")

    @patch("release.source_revision", return_value="same-source")
    @patch("release.artifact_identity", return_value={"artifact": "digest", "deployed_git_sha": "same-source"})
    def test_already_selected_same_source_is_explicit_no_change(self, identity, revision):
        record = inspect_candidate(self.project, "pilot", "52", "52")
        self.assertEqual(record["status"], "unchanged")
        self.assertFalse(record["evaluation_performed"])
        self.assertFalse(record["promotion_performed"])
        self.project.beta.agents.patch_agent_details.assert_not_called()

    @patch("release.source_revision", return_value="new-source")
    @patch("release.artifact_identity", return_value={"artifact": "digest", "deployed_git_sha": "old-source"})
    def test_unchanged_version_with_wrong_source_is_not_a_success(self, identity, revision):
        with self.assertRaises(ValueError):
            inspect_candidate(self.project, "pilot", "52", "52")

    @patch("release.source_revision", return_value="new-source")
    @patch("release.artifact_identity")
    def test_new_version_still_requires_evaluation(self, identity, revision):
        identity.side_effect = lambda _p, _a, version: {
            "artifact": f"digest-{version}", "deployed_git_sha": "new-source", "definition_sha256": "d"}
        record = inspect_candidate(self.project, "pilot", "52", "53")
        self.assertEqual(record["status"], "candidate")
        self.assertFalse(record["equivalent_artifact"])

    @patch("release.source_revision", return_value="ci-only-change")
    @patch("release.artifact_identity")
    def test_byte_identical_new_version_is_not_reevaluated(self, identity, revision):
        identity.side_effect = lambda _p, _a, version: {
            "artifact": "same-digest", "deployed_git_sha": "ci-only-change" if version == "53" else "old",
            "definition_sha256": "same-definition"}
        record = inspect_candidate(self.project, "pilot", "52", "53")
        self.assertEqual(record["status"], "unchanged")
        self.assertTrue(record["equivalent_artifact"])
        identity.side_effect = lambda _p, _a, version: {
            "artifact": "same-digest", "deployed_git_sha": "ci-only-change",
            "definition_sha256": "changed-env" if version == "53" else "same-definition"}
        self.assertEqual(inspect_candidate(self.project, "pilot", "52", "53")["status"], "candidate")

    def test_regression_never_changes_routing(self):
        self.candidate["output_items"][0]["results"][0]["score"] = 4
        with self.assertRaises(ValueError):
            promote(self.project, "https://example.test", "pilot", self.baseline, self.candidate)
        self.project.beta.agents.patch_agent_details.assert_not_called()

    def test_stale_baseline_never_changes_routing(self):
        self.project.agents.get.return_value.as_dict.return_value["agent_endpoint"]["version_selector"] = selection("54")
        with self.assertRaises(ValueError):
            promote(self.project, "https://example.test", "pilot", self.baseline, self.candidate)
        self.project.beta.agents.patch_agent_details.assert_not_called()

    def test_wrong_target_never_changes_routing(self):
        with self.assertRaises(ValueError):
            promote(self.project, "https://wrong.test", "pilot", self.baseline, self.candidate)
        self.project.beta.agents.patch_agent_details.assert_not_called()

    @patch("release.source_revision", return_value="git-test")
    @patch("release.artifact_identity", return_value={"artifact": "digest", "definition_sha256": "hash", "deployed_git_sha": "git-test"})
    def test_exact_candidate_promoted_without_rebuild(self, identity, revision):
        self.candidate["provenance"].update(identity.return_value, evaluation_git_sha="git-test")
        self.project.agents.get_version.return_value.as_dict.return_value = {"status": "active"}
        self.project.agents.get.return_value.as_dict.side_effect = [
            {"agent_endpoint": {"version_selector": selection("52")}},
            {"agent_endpoint": {"version_selector": selection("53")}},
        ]
        promote(self.project, "https://example.test", "pilot", self.baseline, self.candidate)
        self.project.beta.agents.patch_agent_details.assert_called_once_with(
            "pilot", body={"agent_endpoint": {
                "protocols": ["invocations"], "version_selector": selection("53")
            }}
        )

    @patch("release.invoke", side_effect=RuntimeError("stream failed"))
    @patch("release.pin")
    @patch("release.route", return_value="53")
    def test_smoke_failure_rolls_back(self, routing, pin, invoke):
        with self.assertRaisesRegex(RuntimeError, "stream failed"):
            smoke_or_rollback(self.project, Mock(), "https://example.test", "pilot", "52", "53")
        pin.assert_called_once_with(self.project, "pilot", "52")

    @patch("release.invoke", return_value={"text": "healthy response", "tool_calls": [],
                                           "tool_results": {}, "tool_definitions": None})
    @patch("release.pin")
    @patch("release.route", return_value="53")
    def test_smoke_success_does_not_roll_back(self, routing, pin, invoke):
        smoke_or_rollback(self.project, Mock(), "https://example.test", "pilot", "52", "53")
        pin.assert_not_called()
        self.assertFalse(invoke.call_args.kwargs["include_tool_events"])

    @patch("release.artifact_identity", return_value={"artifact": "different"})
    def test_artifact_mismatch_blocks_promotion(self, identity):
        with self.assertRaisesRegex(ValueError, "artifact"):
            promote(self.project, "https://example.test", "pilot", self.baseline, self.candidate)
        self.project.beta.agents.patch_agent_details.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class CandidateMetadataTests(unittest.TestCase):
    def test_pr_candidates_are_marked_throwaway(self):
        import tempfile
        from pathlib import Path
        import yaml
        from release import prepare
        project = Mock()
        with patch("release.route", return_value="7"), patch("release.source_revision", return_value="c" * 40), \
                tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "agent.yaml"
            manifest.write_text("name: pilot\n")
            self.assertEqual(prepare(project, "pilot", manifest, "42"), "7")
            metadata = yaml.safe_load(manifest.read_text())["metadata"]
            self.assertEqual((metadata["agentops_candidate"], metadata["agentops_pr"]), ("true", "42"))
            manifest.write_text("name: pilot\n")
            prepare(project, "pilot", manifest)
            self.assertNotIn("agentops_candidate", yaml.safe_load(manifest.read_text())["metadata"])
            with self.assertRaises(ValueError):
                prepare(project, "pilot", manifest, "42; rm")
