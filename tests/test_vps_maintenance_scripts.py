"""Offline regression tests for VPS search synchronization maintenance scripts."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")
_BASH_CANDIDATES = (
    (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash"))
    if os.name == "nt"
    else (shutil.which("bash"),)
)
BASH = next(
    (candidate for candidate in _BASH_CANDIDATES if candidate and Path(candidate).is_file()),
    None,
)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = False,
    timeout: float = 20,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    assert GIT is not None
    return run([GIT, *arguments], cwd=cwd, check=True)


@unittest.skipUnless(PWSH, "PowerShell 7 is required")
class PowerShellMaintenanceTests(unittest.TestCase):
    def test_shell_literal_round_trips_as_one_argument_without_execution(self) -> None:
        dangerous_value = "/tmp/Job App's;$(touch should-not-exist);`echo unsafe`"
        encoded_value = base64.b64encode(dangerous_value.encode()).decode()
        command = (
            f"$value=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded_value}'));"
            f". '{SCRIPTS / 'vps_script_helpers.ps1'}';"
            "ConvertTo-PosixShellLiteral $value"
        )

        result = run([PWSH, "-NoProfile", "-Command", command], cwd=ROOT, check=True)
        literal = result.stdout.strip()

        self.assertEqual(
            literal,
            "'/tmp/Job App'\"'\"'s;$(touch should-not-exist);`echo unsafe`'",
        )
        if BASH:
            with tempfile.TemporaryDirectory() as temporary_directory:
                marker = Path(temporary_directory) / "should-not-exist"
                round_trip = run(
                    [BASH, "-c", f"set -- {literal}; printf '%s' \"$1\""],
                    cwd=Path(temporary_directory),
                    check=True,
                )
                self.assertEqual(round_trip.stdout, dangerous_value)
                self.assertFalse(marker.exists())

    def test_shell_literal_rejects_newlines(self) -> None:
        command = (
            f". '{SCRIPTS / 'vps_script_helpers.ps1'}';ConvertTo-PosixShellLiteral \"line1`nline2\""
        )

        result = run([PWSH, "-NoProfile", "-Command", command], cwd=ROOT)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newline", result.stderr)

    def test_freshness_compares_unrounded_age_and_rejects_future_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            report = directory / "coverage.json"
            script = str(SCRIPTS / "check_sync_freshness.ps1")

            report.write_text(
                json.dumps(
                    {
                        "generated_at": (
                            datetime.now(timezone.utc) - timedelta(seconds=1)
                        ).isoformat()
                    }
                ),
                encoding="utf-8",
            )
            stale = run(
                [PWSH, "-NoProfile", "-File", script, "-Path", str(report), "-ThresholdHours", "0"],
                cwd=directory,
            )
            self.assertEqual(stale.returncode, 1)
            self.assertIn("STALE", stale.stdout + stale.stderr)

            report.write_text(
                json.dumps(
                    {
                        "generated_at": (
                            datetime.now(timezone.utc) + timedelta(minutes=10)
                        ).isoformat()
                    }
                ),
                encoding="utf-8",
            )
            future = run(
                [PWSH, "-NoProfile", "-File", script, "-Path", str(report)],
                cwd=directory,
            )
            self.assertEqual(future.returncode, 1)
            self.assertIn("future", future.stderr)

            report.write_text(
                json.dumps({"generated_at": datetime.now(timezone.utc).isoformat()}),
                encoding="utf-8",
            )
            fresh = run(
                [PWSH, "-NoProfile", "-File", script, "-Path", str(report)],
                cwd=directory,
            )
            self.assertEqual(fresh.returncode, 0)
            self.assertIn("OK", fresh.stdout)

    def test_pruner_rejects_negative_age_and_treats_directory_literally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            literal_output = directory / "output[one]"
            literal_output.mkdir()
            candidate = literal_output / "Example_Resume.pdf"
            candidate.write_bytes(b"generated")
            old_timestamp = time.time() - (2 * 24 * 60 * 60)
            os.utime(candidate, (old_timestamp, old_timestamp))
            script = str(SCRIPTS / "prune_old_outputs.ps1")

            invalid = run(
                [
                    PWSH,
                    "-NoProfile",
                    "-File",
                    script,
                    "-OutputDir",
                    str(literal_output),
                    "-Days",
                    "-1",
                ],
                cwd=directory,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertTrue(candidate.exists())

            dry_run = run(
                [
                    PWSH,
                    "-NoProfile",
                    "-File",
                    script,
                    "-OutputDir",
                    str(literal_output),
                    "-Days",
                    "1",
                ],
                cwd=directory,
            )
            self.assertEqual(dry_run.returncode, 0)
            self.assertIn("DRY RUN", dry_run.stdout)
            self.assertTrue(candidate.exists())

    @unittest.skipUnless(os.name == "nt", "Plink command discovery test is Windows-specific")
    def test_trigger_uses_temporary_password_file_and_quotes_remote_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            mock_directory = directory / "mock-bin"
            mock_directory.mkdir()
            capture_path = directory / "plink-capture.json"
            config_path = directory / "vps.json"
            marker_path = directory / "injected"
            password = "sentinel password"
            config_path.write_text(
                json.dumps(
                    {
                        "vps": {
                            "host": "example.test",
                            "ssh_user": "tester",
                            "ssh_port": 2222,
                            "ssh_host_key": "ssh-ed25519 255 SHA256:trusted",
                            "ssh_password": {"value": password},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (mock_directory / "plink.ps1").write_text(
                """
$passwordIndex = [Array]::IndexOf($args, "-pwfile")
$passwordPath = $args[$passwordIndex + 1]
$payload = @{
    arguments = @($args)
    password_path = $passwordPath
    password_content = [IO.File]::ReadAllText($passwordPath)
}
[IO.File]::WriteAllText(
    $env:PLINK_CAPTURE,
    ($payload | ConvertTo-Json -Compress),
    [Text.UTF8Encoding]::new($false)
)
exit 23
""".strip(),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PATH"] = str(mock_directory) + os.pathsep + environment["PATH"]
            environment["PLINK_CAPTURE"] = str(capture_path)
            remote_path = f"/root/Job App's;touch {marker_path.as_posix()}"

            result = run(
                [
                    PWSH,
                    "-NoProfile",
                    "-File",
                    str(SCRIPTS / "trigger_vps_search.ps1"),
                    "-RemoteRepoPath",
                    remote_path,
                    "-ConfigPath",
                    str(config_path),
                ],
                cwd=directory,
                env=environment,
            )

            self.assertEqual(result.returncode, 23)
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            arguments = capture["arguments"]
            self.assertIn("-pwfile", arguments)
            self.assertIn("-hostkey", arguments)
            self.assertIn("ssh-ed25519 255 SHA256:trusted", arguments)
            self.assertIn("-P", arguments)
            self.assertIn(2222, arguments)
            self.assertNotIn("-pw", arguments)
            self.assertNotIn(password, arguments)
            self.assertEqual(capture["password_content"], password)
            self.assertFalse(Path(capture["password_path"]).exists())
            self.assertFalse(marker_path.exists())
            self.assertEqual(
                arguments[-1],
                f"exec bash -- '/root/Job App'\"'\"'s;touch {marker_path.as_posix()}"
                "/scripts/vps_search_sync.sh'",
            )


@unittest.skipUnless(PWSH and GIT, "PowerShell 7 and Git are required")
class PullSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.remote = self.root / "remote.git"
        self.publisher = self.root / "publisher"
        self.consumer = self.root / "consumer"
        self.outside = self.root / "outside"
        self.outside.mkdir()

        git(self.root, "init", "--bare", str(self.remote))
        self.publisher.mkdir()
        git(self.publisher, "init", "-b", "vps-search-output")
        git(self.publisher, "config", "user.name", "VPS Test")
        git(self.publisher, "config", "user.email", "vps-test@example.test")
        git(self.publisher, "remote", "add", "origin", str(self.remote))
        self._publish_snapshot(include_cache=True, generation="one")

        self.consumer.mkdir()
        git(self.consumer, "init", "-b", "main")
        git(self.consumer, "config", "user.name", "Consumer Test")
        git(self.consumer, "config", "user.email", "consumer-test@example.test")
        (self.consumer / ".gitignore").write_text("output/*\n", encoding="utf-8")
        git(self.consumer, "add", ".gitignore")
        git(self.consumer, "commit", "-m", "initialize consumer")
        git(self.consumer, "remote", "add", "origin", str(self.remote))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _publish_snapshot(self, *, include_cache: bool, generation: str) -> None:
        output = self.publisher / "output"
        output.mkdir(exist_ok=True)
        (output / "job_search_coverage.json").write_text(
            json.dumps({"generated_at": generation}),
            encoding="utf-8",
        )
        (output / "ai_jobs.csv").write_text(f"generation\n{generation}\n", encoding="utf-8")
        cache = output / "ats_boards_cache.json"
        if include_cache:
            cache.write_text(json.dumps({"generation": generation}), encoding="utf-8")
        elif cache.exists():
            cache.unlink()
        git(self.publisher, "add", "-A")
        git(self.publisher, "commit", "-m", f"publish snapshot {generation}")
        git(self.publisher, "push", "-u", "origin", "vps-search-output")

    def _run_pull(self) -> subprocess.CompletedProcess[str]:
        return run(
            [
                PWSH,
                "-NoProfile",
                "-File",
                str(SCRIPTS / "pull_search_output.ps1"),
                "-RepositoryPath",
                str(self.consumer),
            ],
            cwd=self.outside,
        )

    def test_pull_restores_complete_snapshot_without_touching_index(self) -> None:
        result = self._run_pull()

        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.consumer / "output"
        self.assertEqual(
            json.loads((output / "job_search_coverage.json").read_text(encoding="utf-8")),
            {"generated_at": "one"},
        )
        self.assertIn("one", (output / "ai_jobs.csv").read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads((output / "ats_boards_cache.json").read_text(encoding="utf-8")),
            {"generation": "one"},
        )
        git(self.consumer, "diff", "--cached", "--exit-code")

    def test_incomplete_remote_snapshot_leaves_all_local_files_unchanged(self) -> None:
        output = self.consumer / "output"
        output.mkdir(exist_ok=True)
        sentinels = {
            "job_search_coverage.json": "local coverage",
            "ai_jobs.csv": "local jobs",
            "ats_boards_cache.json": "local cache",
        }
        for name, content in sentinels.items():
            (output / name).write_text(content, encoding="utf-8")
        self._publish_snapshot(include_cache=False, generation="two")

        result = self._run_pull()

        self.assertEqual(result.returncode, 1)
        self.assertIn("incomplete", result.stderr)
        for name, content in sentinels.items():
            self.assertEqual((output / name).read_text(encoding="utf-8"), content)
        git(self.consumer, "diff", "--cached", "--exit-code")


@unittest.skipUnless(BASH, "Bash is required")
class BashMaintenanceTests(unittest.TestCase):
    def test_shell_scripts_parse_and_logrotate_template_renders(self) -> None:
        for name in ("vps_search_sync.sh", "install_vps_logrotate.sh"):
            result = run([BASH, "-n", f"scripts/{name}"], cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)

        rendered = run(
            [BASH, "scripts/install_vps_logrotate.sh", "--stdout"],
            cwd=ROOT,
            check=True,
        ).stdout
        self.assertNotIn("@VPS_SYNC_LOG_PATH@", rendered)
        self.assertIn("vps_sync.log", rendered)
        self.assertEqual(rendered.count("{"), rendered.count("}"))

    @unittest.skipUnless(shutil.which("flock"), "flock is required")
    def test_overlapping_search_run_exits_before_starting_second_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            (repository / "scripts").mkdir()
            shutil.copy2(SCRIPTS / "vps_search_sync.sh", repository / "scripts")
            (repository / ".venv" / "bin").mkdir(parents=True)
            (repository / ".venv" / "bin" / "activate").write_text("", encoding="utf-8")
            fake_bin = repository / "fake-bin"
            fake_bin.mkdir()
            started = repository / "started"
            release = repository / "release"
            count = repository / "count"
            fake_python = fake_bin / "python"
            fake_python.write_text(
                f"""#!/usr/bin/env bash
echo run >> {count!s}
touch {started!s}
while [[ ! -f {release!s} ]]; do sleep 0.05; done
exit 91
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            git(repository, "init")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]

            first = subprocess.Popen(
                [BASH, str(repository / "scripts" / "vps_search_sync.sh")],
                cwd=repository,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 5
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "the first mocked search did not start")

                second = run(
                    [BASH, str(repository / "scripts" / "vps_search_sync.sh")],
                    cwd=repository,
                    env=environment,
                )
                self.assertEqual(second.returncode, 75)
                self.assertIn("already running", second.stderr)
                self.assertEqual(count.read_text(encoding="utf-8").splitlines(), ["run"])
            finally:
                release.touch()
                first.communicate(timeout=5)

    @unittest.skipUnless(shutil.which("flock"), "flock is required")
    def test_publisher_rejects_missing_generated_artifact_before_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            (repository / "scripts").mkdir()
            shutil.copy2(SCRIPTS / "vps_search_sync.sh", repository / "scripts")
            (repository / ".venv" / "bin").mkdir(parents=True)
            (repository / ".venv" / "bin" / "activate").write_text("", encoding="utf-8")
            fake_bin = repository / "fake-bin"
            fake_bin.mkdir()
            fake_python = fake_bin / "python"
            fake_python.write_text(
                """#!/usr/bin/env bash
mkdir -p output
printf '{}\\n' > output/job_search_coverage.json
printf 'title\\n' > output/ai_jobs.csv
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            git(repository, "init")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]

            result = run(
                [BASH, str(repository / "scripts" / "vps_search_sync.sh")],
                cwd=repository,
                env=environment,
            )

            self.assertEqual(result.returncode, 66)
            self.assertIn("ats_boards_cache.json", result.stderr)
            self.assertFalse((repository / ".sync-worktree").exists())
