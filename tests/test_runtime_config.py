from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core.runtime_config import (  # noqa: E402
    DEFAULT_RUNTIME_CONFIG_FILE,
    RUNTIME_CONFIG_FILE,
    load_runtime_config,
    resolve_runtime_path,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_packaged_defaults_match_the_tracked_runtime_config(self) -> None:
        self.assertEqual(
            json.loads(DEFAULT_RUNTIME_CONFIG_FILE.read_text(encoding="utf-8")),
            json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8")),
        )

    def test_tracked_runtime_config_loads_all_shared_operational_settings(self) -> None:
        config = load_runtime_config()

        self.assertEqual(config.browser["cdp_endpoint"], "http://localhost:9222")
        self.assertEqual(config.vertex["project_id"], "from-service-account")
        self.assertGreater(config.ashby["max_submit_attempts"], 0)
        self.assertGreater(config.resume["original_character_count"], 0)
        self.assertEqual(config.application["vps_max_document_jobs"], 10)
        self.assertEqual(config.application["vps_document_retry_jobs"], 2)
        self.assertEqual(config.application["vps_max_attempts_per_ats"], 10)
        self.assertEqual(
            config.application["vps_application_state_file"],
            "output/vps_application_state.json",
        )
        self.assertEqual(
            config.application["vps_application_failure_report"],
            "output/vps_application_failures.json",
        )
        self.assertEqual(
            resolve_runtime_path(config.application["resume_source_file"]),
            ROOT / "data" / "base_resume.txt",
        )
        self.assertEqual(
            resolve_runtime_path(config.application["seo_config_file"]),
            ROOT / "config" / "seo_config.json",
        )

    def test_invalid_runtime_setting_is_rejected_before_workflow_startup(self) -> None:
        document = json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8"))
        document["ashby"]["max_form_steps"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ashby.max_form_steps"):
                load_runtime_config(path)

    def test_cover_letter_section_loads_with_valid_word_budget(self) -> None:
        config = load_runtime_config()

        self.assertGreater(config.cover_letter["max_retries"], 0)
        self.assertGreater(
            config.cover_letter["maximum_words"], config.cover_letter["minimum_words"]
        )

    def test_cover_letter_word_budget_must_be_ordered(self) -> None:
        document = json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8"))
        document["cover_letter"]["maximum_words"] = document["cover_letter"]["minimum_words"]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "maximum_words"):
                load_runtime_config(path)


if __name__ == "__main__":
    unittest.main()
