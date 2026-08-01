from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from job_application_automation.core.runtime_config import (  # noqa: E402
    DEFAULT_RUNTIME_CONFIG_DIR,
    RUNTIME_CONFIG_DIR,
    RUNTIME_SECTION_NAMES,
    load_runtime_config,
    resolve_runtime_path,
)


def _split_document(directory: Path) -> dict[str, object]:
    document = json.loads((directory / "schema_version.json").read_text(encoding="utf-8"))
    for section_name in RUNTIME_SECTION_NAMES:
        section_document = json.loads(
            (directory / f"{section_name}.json").read_text(encoding="utf-8")
        )
        document.update(section_document)
    return document


class RuntimeConfigTests(unittest.TestCase):
    def test_packaged_defaults_match_the_tracked_runtime_config(self) -> None:
        self.assertEqual(
            _split_document(DEFAULT_RUNTIME_CONFIG_DIR), _split_document(RUNTIME_CONFIG_DIR)
        )
        self.assertEqual(
            {path.name for path in DEFAULT_RUNTIME_CONFIG_DIR.glob("*.json")},
            {path.name for path in RUNTIME_CONFIG_DIR.glob("*.json")},
        )

    def test_tracked_runtime_config_loads_all_shared_operational_settings(self) -> None:
        config = load_runtime_config()

        self.assertEqual(config.browser["cdp_endpoint"], "http://localhost:9222")
        self.assertEqual(config.vertex["project_id"], "from-service-account")
        self.assertGreater(config.ashby["max_submit_attempts"], 0)
        self.assertGreaterEqual(config.ashby["continuous_sleep_min_seconds"], 900)
        self.assertGreaterEqual(
            config.ashby["continuous_sleep_max_seconds"],
            config.ashby["continuous_sleep_min_seconds"],
        )
        self.assertEqual(config.ashby["continuous_application_limit"], 12)
        self.assertEqual(config.ashby["continuous_application_window_seconds"], 86_400)
        self.assertEqual(config.ashby["spam_rejection_cooldown_seconds"], 86_400)
        self.assertEqual(config.ashby["spam_rejection_threshold"], 1)
        self.assertEqual(config.ashby["submission_result_timeout_seconds"], 15)
        self.assertEqual(config.ashby["submission_result_poll_seconds"], 0.5)
        self.assertGreater(config.gmail["greenhouse_security_code_poll_timeout_seconds"], 30)
        self.assertGreater(config.resume["original_character_count"], 0)
        self.assertIn("greenhouse", config.search["ats_hosts"])
        self.assertIn("AI", config.search["ai_terms"])
        self.assertEqual(config.search["defaults"]["max_discovery_queries"], 400)
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
            config.application["vps_job_backlog_file"],
            "output/job_backlog.json",
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
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["ashby"]["max_form_steps"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ashby.max_form_steps"):
                load_runtime_config(path)

    def test_invalid_search_setting_is_rejected_before_workflow_startup(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["search"]["defaults"]["results_per_query"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "search.defaults.results_per_query"):
                load_runtime_config(path)

    def test_older_schema_one_config_remains_valid_without_new_ashby_controls(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        for key in (
            "continuous_sleep_min_seconds",
            "continuous_sleep_max_seconds",
            "continuous_application_limit",
            "continuous_application_window_seconds",
            "spam_rejection_cooldown_seconds",
            "spam_rejection_threshold",
            "submission_result_timeout_seconds",
            "submission_result_poll_seconds",
            "submission_spam_phrases",
        ):
            document["ashby"].pop(key)
        document["browser"]["cdp_endpoint"] = "http://127.0.0.1:9333"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            config = load_runtime_config(path)

        self.assertEqual(config.browser["cdp_endpoint"], "http://127.0.0.1:9333")
        self.assertNotIn("continuous_application_limit", config.ashby)

    def test_zero_continuous_application_limit_disables_the_optional_cap(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["ashby"]["continuous_application_limit"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            config = load_runtime_config(path)

        self.assertEqual(config.ashby["continuous_application_limit"], 0)

    def test_cover_letter_section_loads_with_valid_word_budget(self) -> None:
        config = load_runtime_config()

        self.assertGreater(config.cover_letter["max_retries"], 0)
        self.assertGreater(
            config.cover_letter["maximum_words"], config.cover_letter["minimum_words"]
        )

    def test_cover_letter_word_budget_must_be_ordered(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)
        document["cover_letter"]["maximum_words"] = document["cover_letter"]["minimum_words"]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "maximum_words"):
                load_runtime_config(path)

    def test_split_config_rejects_missing_section_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            split_dir = Path(directory)
            for source in RUNTIME_CONFIG_DIR.glob("*.json"):
                if source.name != "gmail.json":
                    (split_dir / source.name).write_text(
                        source.read_text(encoding="utf-8"), encoding="utf-8"
                    )

            with self.assertRaisesRegex(ValueError, "missing: gmail.json"):
                load_runtime_config(split_dir)

    def test_explicit_legacy_monolithic_config_remains_supported(self) -> None:
        document = _split_document(RUNTIME_CONFIG_DIR)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            config = load_runtime_config(path)

        self.assertEqual(config.browser["cdp_endpoint"], "http://localhost:9222")


if __name__ == "__main__":
    unittest.main()
