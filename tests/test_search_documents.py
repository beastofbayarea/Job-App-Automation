from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_application_automation.core import search_documents


class SearchDocumentTests(unittest.TestCase):
    def test_generates_only_live_unarchived_jobs_and_persists_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "jobs.json"
            profile_path = root / "profile.json"
            state_path = root / "state.json"
            config_path = root / "vps.json"
            launcher = root / "job_automation.py"
            input_path.write_text(
                json.dumps(
                    [
                        {
                            "job_url": "https://jobs.example.test/live",
                            "company": "Example",
                            "title": "Product Manager",
                            "description": "Build AI products.",
                            "location": "Remote",
                            "live_status": "live",
                        },
                        {
                            "job_url": "https://jobs.example.test/closed",
                            "company": "Example",
                            "title": "Product Manager",
                            "description": "Closed role.",
                            "live_status": "closed",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            profile_path.write_text(
                json.dumps(
                    {"candidate": {"contact": {"fallback_email": "Candidate@Example.test"}}}
                ),
                encoding="utf-8",
            )
            config_path.write_text("{}", encoding="utf-8")
            launcher.write_text("", encoding="utf-8")

            with patch.object(search_documents.subprocess, "run") as run:
                run.return_value.returncode = 0
                result = search_documents.main(
                    [
                        "--input",
                        str(input_path),
                        "--profile",
                        str(profile_path),
                        "--vps-config",
                        str(config_path),
                        "--state",
                        str(state_path),
                        "--launcher",
                        str(launcher),
                    ]
                )

            self.assertEqual(result, 0)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertIn("candidate@example.test", command)
            self.assertIn("--archive", command)
            self.assertIn("--overwrite", command)
            self.assertFalse(Path(command[command.index("--jd-file") + 1]).exists())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["jobs"]["https://jobs.example.test/live"]["status"],
                "archived",
            )

            with patch.object(search_documents.subprocess, "run") as second_run:
                self.assertEqual(
                    search_documents.main(
                        [
                            "--input",
                            str(input_path),
                            "--profile",
                            str(profile_path),
                            "--vps-config",
                            str(config_path),
                            "--state",
                            str(state_path),
                            "--launcher",
                            str(launcher),
                        ]
                    ),
                    0,
                )
                second_run.assert_not_called()

    def test_failed_generation_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "jobs.json"
            profile_path = root / "profile.json"
            state_path = root / "state.json"
            input_path.write_text(
                json.dumps(
                    [
                        {
                            "job_url": "https://jobs.example.test/live",
                            "company": "Example",
                            "title": "Product Manager",
                            "description": "Build AI products.",
                            "live_status": "live",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            profile_path.write_text(
                json.dumps(
                    {"candidate": {"contact": {"fallback_email": "candidate@example.test"}}}
                ),
                encoding="utf-8",
            )

            with patch.object(search_documents.subprocess, "run") as run:
                run.return_value.returncode = 7
                self.assertEqual(
                    search_documents.main(
                        [
                            "--input",
                            str(input_path),
                            "--profile",
                            str(profile_path),
                            "--vps-config",
                            str(root / "vps.json"),
                            "--state",
                            str(state_path),
                        ]
                    ),
                    1,
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["jobs"]["https://jobs.example.test/live"]["status"], "failed")
