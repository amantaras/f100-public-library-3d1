import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from report_ci_failure import (
    GitHub, evidence_for_run, issue_body, load_artifact, markers,
    primary_failure, safe_text, upsert_issue, validate_run,
)

REPOSITORY = "example/agent"


def run_fixture(run_id=100, attempt=1):
    return {
        "id": run_id, "run_attempt": attempt, "name": "Copilot agent quality gate",
        "path": ".github/workflows/agentops-deploy-dev.yml", "head_branch": "main",
        "event": "push", "status": "completed", "conclusion": "failure", "head_sha": "a" * 40,
    }


def job_fixture(name="evaluate", conclusion="failure"):
    return {"name": name, "conclusion": conclusion, "steps": [
        {"name": "Block any regression or incomplete evaluation", "conclusion": conclusion}
    ]}


class FakeGitHub:
    repository = REPOSITORY

    def __init__(self):
        self.issues = [{"number": 1, "state": "open", "body": "Human domain-review issue"}]
        self.comments = {}
        self.posts = []
        self.label_exists = False

    def pages(self, path, key=None):
        if path.startswith("/issues?"):
            return iter(self.issues)
        number = int(path.split("/")[2])
        return iter(self.comments.get(number, []))

    def object(self, method, path, body=None):
        if method == "GET" and path.startswith("/labels/"):
            if not self.label_exists:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return {"name": "ci-cd"}
        self.posts.append((path, body))
        if path == "/labels":
            self.label_exists = True
            return body
        if path == "/issues":
            issue = {**body, "number": len(self.issues) + 1, "state": "open"}
            self.issues.append(issue)
            return issue
        number = int(path.split("/")[2])
        self.comments.setdefault(number, []).append(body)
        return body


class IssueReportingTests(unittest.TestCase):
    def setUp(self):
        self.run = run_fixture()
        self.job = job_fixture()
        self.body = issue_body(REPOSITORY, self.run, self.job, [self.job], {}, [])

    def test_primary_failure_prevents_downstream_duplicates(self):
        jobs = [job_fixture("readiness"), job_fixture("evaluate"), job_fixture("promote", "skipped")]
        self.assertEqual(primary_failure(jobs)["name"], "evaluate")
        self.assertEqual(primary_failure(jobs + [job_fixture("test")])["name"], "test")

    def test_skipped_cancelled_and_reporter_jobs_are_not_actionable(self):
        self.assertIsNone(primary_failure([
            job_fixture("evaluate", "cancelled"), job_fixture("promote", "skipped"),
            job_fixture("test", "success"), job_fixture("report-failure / report"),
        ]))

    def test_timeout_and_custom_stage_names_are_reportable(self):
        self.assertEqual(primary_failure([job_fixture("deployment smoke", "timed_out")])["name"],
                         "deployment smoke")

    def test_fingerprint_is_stable_but_occurrence_tracks_attempts(self):
        original = markers(REPOSITORY, self.run, self.job)
        later = markers(REPOSITORY, run_fixture(101), self.job)
        retry = markers(REPOSITORY, run_fixture(100, 2), self.job)
        self.assertEqual(original[0], later[0])
        self.assertEqual(original[0], retry[0])
        self.assertNotEqual(original[1], later[1])
        self.assertNotEqual(original[1], retry[1])
        self.assertNotEqual(original[0], markers(REPOSITORY, self.run, job_fixture("test"))[0])

    def test_creates_bot_tracking_issue_without_touching_manual_issue(self):
        api = FakeGitHub()
        action, url = upsert_issue(api, self.run, self.job, self.body)
        self.assertEqual(action, "created")
        self.assertTrue(url.endswith("/issues/2"))
        self.assertEqual(api.issues[0]["body"], "Human domain-review issue")
        self.assertEqual(api.issues[-1]["labels"], ["ci-cd"])

    def test_same_run_replay_is_idempotent(self):
        api = FakeGitHub()
        upsert_issue(api, self.run, self.job, self.body)
        writes = len(api.posts)
        self.assertEqual(upsert_issue(api, self.run, self.job, self.body)[0], "already-reported")
        self.assertEqual(len(api.posts), writes)

    def test_new_run_comments_once_on_existing_open_issue(self):
        api = FakeGitHub()
        upsert_issue(api, self.run, self.job, self.body)
        later = run_fixture(101)
        body = issue_body(REPOSITORY, later, self.job, [self.job], {}, [])
        self.assertEqual(upsert_issue(api, later, self.job, body)[0], "updated")
        self.assertEqual(len(api.issues), 2)
        self.assertEqual(upsert_issue(api, later, self.job, body)[0], "already-reported")
        self.assertEqual(len(api.comments[2]), 1)

    def test_closed_issue_is_not_reopened_or_modified(self):
        api = FakeGitHub()
        upsert_issue(api, self.run, self.job, self.body)
        api.issues[-1]["state"] = "closed"
        self.assertEqual(upsert_issue(api, run_fixture(101), self.job, self.body)[0], "created")
        self.assertEqual(api.issues[1]["state"], "closed")
        self.assertFalse(api.comments)

    def test_report_omits_prompts_responses_judge_text_and_raw_errors(self):
        private = "PRIVATE_PATIENT_TOKEN"
        evidence = {
            "gate": {"passed": False, "failures": [f"{private} below absolute quality floor"],
                     "metrics": {"expected_behavior": {"baseline_mean": 0.8, "candidate_mean": 0.9,
                                                       "baseline_pass_rate": 0.8, "candidate_pass_rate": 0.9}}},
            "candidate": {"provenance": {"version": "59"}, "run": {"status": "completed", "error": private},
                          "output_items": [{"datasource_item": {"case_id": "11", "query": private,
                                                                "response": private},
                                            "results": [{"name": "expected_behavior", "score": 0,
                                                         "passed": False, "reason": private}]}]},
            "readiness": {"status": "blocked", "blockers": [private], "checks": [
                {"name": "Latest eval gate", "status": "blocked", "summary": private},
            ]},
        }
        body = issue_body(REPOSITORY, self.run, self.job, [self.job], evidence, [])
        self.assertNotIn(private, body)
        self.assertIn("Case `11`", body)
        self.assertIn("`59`", body)
        self.assertIn("90.0%", body)
        self.assertIn("absolute quality floor", body)
        self.assertIn("close manually", body)

    def test_auth_failure_is_not_presented_as_agent_regression(self):
        body = issue_body(REPOSITORY, self.run, self.job, [self.job], {
            "candidate": {"failure_kind": "evaluation_authentication", "run": {"status": "failed"}},
        }, [])
        self.assertIn("Scoring authentication/permissions failed; no agent-quality verdict", body)

    def test_missing_and_invalid_artifacts_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, notes = evidence_for_run(root, self.run)
            self.assertIsNone(evidence["gate"])
            self.assertTrue(any("gate.json is unavailable" in note for note in notes))
            bad = root / "broken.json"
            bad.write_text("not JSON")
            self.assertIsNone(load_artifact(bad, notes))
            self.assertIn("no scores were inferred", notes[-1])

    def test_scheduled_artifact_layout_is_supported(self):
        scheduled = {**self.run, "path": ".github/workflows/agentops-doctor.yml"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readiness-100/release/latest/evidence.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"status": "blocked", "checks": []}))
            evidence, notes = evidence_for_run(Path(directory), scheduled)
            self.assertEqual(evidence["readiness"]["status"], "blocked")
            self.assertEqual(notes, [])

    def test_untrusted_sources_are_rejected(self):
        for changes in (
            {"head_branch": "feature"}, {"event": "pull_request"},
            {"path": ".github/workflows/report-ci-failure.yml"},
            {"id": 99}, {"run_attempt": 0}, {"status": "in_progress"},
        ):
            with self.subTest(changes=changes), patch.dict(os.environ, {"GITHUB_RUN_ID": "200"}):
                with self.assertRaises(ValueError):
                    validate_run({**self.run, **changes}, 100)

    def test_current_caller_run_can_be_in_progress(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "100"}):
            validate_run({**self.run, "status": "in_progress"}, 100)

    def test_github_pagination_is_not_limited_to_first_page(self):
        api = GitHub(REPOSITORY, "test-token")
        with patch.object(api, "request", side_effect=[{"jobs": [{"id": 1}] * 100},
                                                        {"jobs": [{"id": 2}]}]) as request:
            results = list(api.pages("/actions/runs/100/jobs", "jobs"))
            self.assertEqual(len(results), 101)
            self.assertIn("page=2", request.call_args.args[1])

    def test_api_errors_are_not_converted_to_success(self):
        api = FakeGitHub()
        with patch.object(api, "object", side_effect=urllib.error.HTTPError("", 403, "Forbidden", {}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                upsert_issue(api, self.run, self.job, self.body)

    def test_metadata_escaping_and_redaction(self):
        text = safe_text("@team\n<details> Bearer abc123 password=secret")
        self.assertNotIn("abc123", text)
        self.assertNotIn("password=secret", text)
        self.assertIn("\\@team", text)
        self.assertNotIn("\n", text)


if __name__ == "__main__":
    unittest.main()
