from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import engine_shared  # noqa: E402


class EngineSharedConfigTests(unittest.TestCase):
    def test_schema_v2_profile_flattens_policy_groups_without_losing_fields(self) -> None:
        config = {
            "schema_version": 2,
            "candidate": {
                "identity": {"first_name": "First", "last_name": "Last"},
                "contact": {"fallback_email": "candidate@example.test"},
                "availability": {"start_date_offset_days": 0},
                "education": [],
            },
            "policies": {
                "answers": {"right to work": "Yes"},
                "eeo": {"gender": "Prefer not to disclose"},
                "matchers": {"email": ["email address"]},
                "option_variants": {"yes": ["Yes"]},
                "explicit_answers": {"will you relocate": "No"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            normalized = engine_shared.load_json_config(path)

        candidate = normalized["candidate"]
        self.assertEqual(candidate["first_name"], "First")
        self.assertEqual(candidate["fallback_email"], "candidate@example.test")
        self.assertEqual(candidate["screening_answers"]["will you relocate"], "No")
        self.assertTrue(candidate["available_start_date"])
        self.assertEqual(normalized["rules"]["right to work"], "Yes")
        self.assertEqual(normalized["eeo_defaults"]["gender"], "Prefer not to disclose")

    def test_engine_result_keeps_legacy_payload_shape(self) -> None:
        payload = engine_shared.engine_result(
            "PREFILLED_ONLY",
            ats="lever",
            is_live=False,
            extra={"screenshot": "proof.png"},
        )

        self.assertEqual(payload["status"], "PREFILLED_ONLY")
        self.assertFalse(payload["submitted"])
        self.assertTrue(payload["test_mode"])
        self.assertEqual(payload["screenshot"], "proof.png")


if __name__ == "__main__":
    unittest.main()
