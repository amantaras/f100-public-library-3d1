import json
from pathlib import Path
import tempfile
import unittest

from production_summary import render


class ProductionSummaryTests(unittest.TestCase):
    def test_missing_record_says_routing_was_not_changed(self):
        self.assertIn("did not change production routing", render(Path("/nonexistent"), Path("/nonexistent")))

    def test_promoted_record_shows_identity_and_smoke(self):
        record = {"status": "promoted", "candidate_version": "2", "selected_version": "2", "previous_version": "1",
                  "evaluated_in": {"version": "69"},
                  "identity": {"artifact": "a" * 64, "deployed_git_sha": "b" * 40},
                  "toolbox": {"toolbox": "f100-public-library-3d1-toolbox:1", "toolbox_sha256": "c" * 64},
                  "smoke": {"routing_tools": ["f100_public_library_3d1_fine_fees"], "tools_exposed": ["f100_public_library_3d1_fine_fees"]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deployment.json"
            path.write_text(json.dumps(record))
            text = render(path, Path(tmp) / "missing.md")
        self.assertIn("`promoted`", text)
        self.assertIn("Evaluated in dev as:** v69", text)
        self.assertIn("f100_public_library_3d1_fine_fees", text)


if __name__ == "__main__":
    unittest.main()
