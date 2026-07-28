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
        self.assertEqual(
            resolve_runtime_path(config.application["resume_source_file"]),
            ROOT / "data" / "base_resume.txt",
        )

    def test_invalid_runtime_setting_is_rejected_before_workflow_startup(self) -> None:
        document = json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8"))
        document["ashby"]["max_form_steps"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_config.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "ashby.max_form_steps"):
                load_runtime_config(path)


if __name__ == "__main__":
    unittest.main()
