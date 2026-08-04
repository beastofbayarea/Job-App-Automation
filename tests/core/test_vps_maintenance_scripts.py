"""Offline regression tests for VPS maintenance scripts."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
PWSH = shutil.which("pwsh")
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
    timeout: float = 60,
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


@unittest.skipUnless(PWSH, "PowerShell 7 is required")
class PowerShellMaintenanceTests(unittest.TestCase):
    def test_failed_job_requeue_tools_refuse_exhausted_fixing_attempts(self) -> None:
        for name in (
            "requeue_vps_greenhouse_failed_json_targets.ps1",
            "requeue_vps_greenhouse_failed_json_fleet.ps1",
            "resume_vps_greenhouse_failed_json_worker.ps1",
        ):
            script = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertIn("skipped_after_fixing_attempts", script, name)
            self.assertIn("fixing_attempts", script, name)
            self.assertIn(">= 2", script, name)
            self.assertIn("_job_identity", script, name)

        resume = (
            SCRIPTS / "resume_vps_greenhouse_failed_json_worker.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("[switch]$StopOnly", resume)
        self.assertIn("systemctl stop '$Unit'", resume)
        self.assertLess(
            resume.index('record.get("retry_policy_status")'),
            resume.index('del state["jobs"][key]'),
        )
        self.assertLess(
            resume.index('atomic_write_text(claims_path'),
            resume.index('del state["jobs"][key]'),
        )

        fleet = (
            SCRIPTS / "requeue_vps_greenhouse_failed_json_fleet.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("trap 'systemctl start $UnitNames' EXIT", fleet)
        self.assertIn("trap - EXIT", fleet)

    def test_failed_job_retry_audit_supports_concise_policy_summary(self) -> None:
        script = (
            SCRIPTS / "audit_vps_greenhouse_failed_json_retry_queue.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[switch]$SummaryOnly", script)
        self.assertIn("fixing_attempt_count_distribution", script)
        self.assertIn("awaiting_remediation_count", script)

    def test_runtime_audit_is_read_only_and_covers_persistent_workloads(self) -> None:
        script = (SCRIPTS / "audit_vps_runtime.ps1").read_text(encoding="utf-8")

        self.assertIn("list-units --type=service --state=running", script)
        self.assertIn("list-unit-files --type=service --state=enabled", script)
        self.assertIn("list-timers --all", script)
        self.assertIn("crontab -l", script)
        self.assertIn("LONGEST_LIVED_PROCESSES", script)
        self.assertIn("LISTENING_SOCKETS", script)
        self.assertIn("docker ps --no-trunc", script)
        self.assertIn("JOB_APP_UNITS", script)
        self.assertIn("MemoryPeak", script)
        self.assertIn("MemoryHigh", script)
        self.assertIn("MemoryMax", script)
        self.assertIn("CPUWeight", script)
        self.assertIn("StartLimitBurst", script)
        self.assertIn("vps-dashboard.service", script)
        self.assertIn("APPLICATION_SERVICE_DIAGNOSTICS", script)
        self.assertIn("nginx -t", script)
        self.assertIn("nginx virtual-host routing", script)
        self.assertIn("REBOOT_REQUIRED", script)
        self.assertIn("-hostkey", script)
        self.assertIn("-pwfile", script)
        self.assertNotIn("systemctl restart", script)
        self.assertNotIn("systemctl stop", script)
        self.assertNotIn("systemctl disable", script)

    def test_dashboard_installer_provisions_public_loopback_and_restores_nginx(self) -> None:
        installer = (SCRIPTS / "install_vps_dashboard.ps1").read_text(encoding="utf-8")
        unit = (SCRIPTS / "templates" / "job-app-dashboard.service.template").read_text(
            encoding="utf-8"
        )

        self.assertIn("-hostkey", installer)
        self.assertIn("-pwfile", installer)
        self.assertIn("systemctl restart vps-dashboard.service", installer)
        self.assertIn("nginx -t", installer)
        self.assertIn("systemctl restart nginx.service", installer)
        self.assertIn("--host 127.0.0.1 --port 8000", unit)
        self.assertIn("MemoryMax=192M", unit)
        self.assertNotIn("--host 0.0.0.0", unit)

        # Dashboard is completely unauthenticated and serves no password authentication.
        self.assertNotIn("JOB_APP_DASHBOARD_ADMIN_PASSWORD", installer)
        self.assertNotIn("dashboard.env", installer)
        self.assertNotIn("EnvironmentFile=-", unit)

        remote_command_body = installer.split('$RemoteCommand = @"', 1)[1].split('"@', 1)[0]
        render_command = (
            f". '{SCRIPTS / 'lib' / 'vps_script_helpers.ps1'}';"
            "$Repo=ConvertTo-PosixShellLiteral '/root/Job-App-Automation';"
            "$UnitStage=ConvertTo-PosixShellLiteral '/tmp/vps-dashboard.service';"
            '$RemoteCommand=@"'
            f"{remote_command_body}"
            '"@;'
            "$RemoteCommand"
        )

        rendered = run([PWSH, "-NoProfile", "-Command", render_command], cwd=ROOT, check=True)
        config_directory_command = next(
            line for line in rendered.stdout.splitlines() if "install -d -m 0700" in line
        )
        self.assertEqual(config_directory_command, 'install -d -m 0700 "$repo/config"')

    def test_backend_quarantine_requires_live_failure_evidence(self) -> None:
        script = (SCRIPTS / "quarantine_unhealthy_cent_backend.ps1").read_text(encoding="utf-8")

        self.assertIn("systemctl is-active --quiet", script)
        self.assertIn("--property=UnitFileState --value", script)
        self.assertIn("--property=ActiveState --value", script)
        self.assertIn("8080", script)
        self.assertIn("password authentication failed", script)
        self.assertIn("too many authentication failures", script)
        self.assertIn('systemctl stop --no-block "$service"', script)
        self.assertIn('systemctl disable "$service"', script)
        self.assertIn("systemctl kill --kill-whom=all --signal=SIGKILL", script)
        self.assertIn("-hostkey", script)
        self.assertIn("-pwfile", script)
        self.assertLess(
            script.index('journalctl -u "$service"'),
            script.index('systemctl stop --no-block "$service"'),
        )

    def test_memory_guard_is_bounded_idempotent_and_preserves_existing_paths(self) -> None:
        script = (SCRIPTS / "install_vps_memory_guard.ps1").read_text(encoding="utf-8")

        self.assertIn("[ValidateRange(512, 4096)]", script)
        self.assertIn("swapon --noheadings --show=NAME", script)
        self.assertIn("Insufficient disk headroom", script)
        self.assertIn("Refusing to overwrite non-swap path", script)
        self.assertIn("chmod 0600", script)
        self.assertIn("mkswap", script)
        self.assertIn("/swapfile none swap sw 0 0", script)
        self.assertIn("vm.swappiness=10", script)
        self.assertIn("99-job-app-memory.conf", script)
        self.assertIn("-hostkey", script)
        self.assertIn("-pwfile", script)

    def test_runtime_restart_discovers_enabled_repository_services(self) -> None:
        script = (SCRIPTS / "restart_vps_runtime.ps1").read_text(encoding="utf-8")

        self.assertIn("systemctl list-unit-files 'job-app-*.service'", script)
        self.assertIn("systemctl list-unit-files 'vps-dashboard.service'", script)
        self.assertIn("RUNTIME_SECTION_NAMES", script)
        self.assertIn("systemctl restart `$units", script)
        self.assertNotIn("assert len(list(RUNTIME_CONFIG_DIR.glob", script)

    def test_code_deployer_is_pinned_bounded_and_fast_forward_only(self) -> None:
        script = (SCRIPTS / "deploy_vps_code.ps1").read_text(encoding="utf-8")
        helper = (SCRIPTS / "lib" / "vps_script_helpers.ps1").read_text(encoding="utf-8")

        self.assertIn("pull --ff-only origin main", script)
        self.assertIn("Invoke-ExternalCommandWithTimeout", script)
        self.assertIn("[int]$TimeoutSeconds = 60", script)
        self.assertIn("-hostkey", script)
        self.assertIn("-pwfile", script)
        self.assertIn("Read-VpsConnectionConfig", script)
        self.assertIn("invalid vps.ssh_port", helper)
        self.assertNotIn("git -C $Repo pull origin main", script)

    def test_status_probe_is_bounded_and_excludes_its_own_process_match(self) -> None:
        script = (SCRIPTS / "check_vps_automation_status.ps1").read_text(encoding="utf-8")

        self.assertIn("[int]$TimeoutSeconds = 30", script)
        self.assertIn("Invoke-ExternalCommandWithTimeout", script)
        self.assertIn("[c]ontinuous-greenhouse", script)
        self.assertIn("[j]ob_automation.py apply", script)
        self.assertIn("job-app-greenhouse.service", script)
        self.assertIn("continuous_greenhouse_state.json", script)
        self.assertIn("CONTINUOUS GREENHOUSE SUMMARY", script)
        self.assertIn("[REDACTED_EMAIL]", script)
        self.assertIn("--email )[[:graph:]]+", script)
        self.assertIn("[REDACTED]", script)
        self.assertNotIn(
            "systemctl --no-pager --full status job-app-greenhouse.service",
            script,
        )

    def test_continuous_greenhouse_installer_delegates_to_generic_supervisor(self) -> None:
        wrapper = (SCRIPTS / "install_vps_continuous_greenhouse.ps1").read_text(encoding="utf-8")
        unit = (SCRIPTS / "templates" / "job-app-continuous-ats.service.template").read_text(
            encoding="utf-8"
        )

        self.assertIn("install_vps_continuous_ats.ps1", wrapper)
        self.assertIn("-AtsPlatform greenhouse", wrapper)
        self.assertIn("job_application_automation.core.continuous_ats", unit)
        self.assertIn("--ats-platform __ATS_PLATFORM__", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("/usr/bin/xvfb-run", unit)

    def test_external_command_timeout_returns_output_exit_code_and_stops_hangs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            mock = directory / "mock-command.ps1"
            mock.write_text(
                """
param([string]$Value)
Write-Output "mock:$Value"
    exit 7
""".strip(),
                encoding="utf-8",
            )
            helper = SCRIPTS / "lib" / "vps_script_helpers.ps1"
            completed_command = (
                f". '{helper}';"
                f"$result=Invoke-ExternalCommandWithTimeout -FilePath '{mock}' "
                "-ArgumentList @('hello') -TimeoutSeconds 30;"
                "$result | ConvertTo-Json -Depth 5 -Compress"
            )

            completed = run(
                [PWSH, "-NoProfile", "-Command", completed_command],
                cwd=directory,
                check=True,
                timeout=60,
            )
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["TimedOut"])
            self.assertEqual(payload["ExitCode"], 7)
            self.assertEqual(payload["Output"], ["mock:hello"])

            marker = directory / "timed-out-command-finished.txt"
            mock.write_text(
                "\n".join(
                    (
                        "param([string]$Marker)",
                        "Start-Sleep -Seconds 30",
                        '[IO.File]::WriteAllText($Marker, "finished")',
                    )
                ),
                encoding="utf-8",
            )
            timeout_command = (
                f". '{helper}';"
                "$stopwatch=[Diagnostics.Stopwatch]::StartNew();"
                f"$result=Invoke-ExternalCommandWithTimeout -FilePath '{mock}' "
                f"-ArgumentList @('{marker}') -TimeoutSeconds 1;"
                "$stopwatch.Stop();"
                "$result | Add-Member -NotePropertyName ElapsedMilliseconds "
                "-NotePropertyValue $stopwatch.ElapsedMilliseconds;"
                "$result | ConvertTo-Json -Depth 5 -Compress"
            )
            timeout_environment = os.environ.copy()
            timeout_environment.update({"TEMP": str(directory), "TMP": str(directory)})
            timed_out = run(
                [PWSH, "-NoProfile", "-Command", timeout_command],
                cwd=directory,
                env=timeout_environment,
                check=True,
                timeout=30,
            )
            payload = json.loads(timed_out.stdout)
            self.assertTrue(payload["TimedOut"])
            self.assertEqual(payload["ExitCode"], 124)
            self.assertFalse(marker.exists())
            self.assertFalse(list(directory.glob("external-command-*")))
            self.assertLess(payload["ElapsedMilliseconds"], 10_000)

    def test_vps_command_transport_normalizes_windows_line_endings(self) -> None:
        expected = "first\nsecond\nthird\n"
        encoded_value = base64.b64encode(b"first\r\nsecond\rthird\n").decode()
        command = (
            f"$value=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded_value}'));"
            f". '{SCRIPTS / 'lib' / 'vps_script_helpers.ps1'}';"
            "$normalized=ConvertTo-LfLineEndings $value;"
            "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($normalized))"
        )

        result = run([PWSH, "-NoProfile", "-Command", command], cwd=ROOT, check=True)

        self.assertEqual(base64.b64decode(result.stdout.strip()).decode(), expected)

    def test_vps_connection_helper_validates_and_defaults_pinned_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "vps.json"
            config = {
                "vps": {
                    "host": "example.test",
                    "ssh_user": "root",
                    "ssh_password": {"value": "test-value"},
                    "ssh_host_key": "ssh-ed25519 test-key",
                }
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            command = (
                f". '{SCRIPTS / 'lib' / 'vps_script_helpers.ps1'}';"
                f"Read-VpsConnectionConfig -Path '{config_path}' | "
                "ConvertTo-Json -Compress"
            )

            result = run([PWSH, "-NoProfile", "-Command", command], cwd=ROOT, check=True)
            connection = json.loads(result.stdout)
            self.assertEqual(connection["Host"], "example.test")
            self.assertEqual(connection["Port"], 22)

            config["vps"]["ssh_port"] = 70_000
            config_path.write_text(json.dumps(config), encoding="utf-8")
            invalid = run([PWSH, "-NoProfile", "-Command", command], cwd=ROOT)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("invalid vps.ssh_port", invalid.stderr)

    def test_direct_plink_installers_normalize_remote_shell_scripts(self) -> None:
        for name in ("install_vps_continuous_ats.ps1",):
            script = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertIn(
                "$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand",
                script,
            )

    def test_shell_literal_round_trips_as_one_argument_without_execution(self) -> None:
        dangerous_value = "/tmp/Job App's;$(touch should-not-exist);`echo unsafe`"
        encoded_value = base64.b64encode(dangerous_value.encode()).decode()
        command = (
            f"$value=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded_value}'));"
            f". '{SCRIPTS / 'lib' / 'vps_script_helpers.ps1'}';"
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
            f". '{SCRIPTS / 'lib' / 'vps_script_helpers.ps1'}';"
            'ConvertTo-PosixShellLiteral "line1`nline2"'
        )

        result = run([PWSH, "-NoProfile", "-Command", command], cwd=ROOT)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newline", result.stderr)

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

    @unittest.skipUnless(os.name == "nt", "PSCP command discovery test is Windows-specific")
    def test_private_report_pull_uses_pinned_auth_and_promotes_complete_report_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            mock_directory = directory / "mock-bin"
            mock_directory.mkdir()
            capture_path = directory / "pscp-capture.jsonl"
            config_path = directory / "vps.json"
            destination = directory / "reports"
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
            (mock_directory / "pscp.ps1").write_text(
                """
$passwordIndex = [Array]::IndexOf($args, "-pwfile")
$passwordPath = $args[$passwordIndex + 1]
$source = [string]$args[$args.Count - 2]
$target = [string]$args[$args.Count - 1]
$payload = @{
    arguments = @($args)
    password_path = $passwordPath
    password_content = [IO.File]::ReadAllText($passwordPath)
}
[IO.File]::AppendAllText(
    $env:PSCP_CAPTURE,
    (($payload | ConvertTo-Json -Compress) + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
)
if ($source.EndsWith("submission_log.json")) {
    [IO.File]::WriteAllText($target, '{"confirmed": {}}')
} elseif ($source.EndsWith("vps_infra_status.json")) {
    [IO.File]::WriteAllText($target, '{"active_services": [], "uptime": "up 1 day"}')
} else {
    [IO.File]::WriteAllText($target, '{"failure_count": 0, "failures": []}')
}
exit 0
""".strip(),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PATH"] = str(mock_directory) + os.pathsep + environment["PATH"]
            environment["PSCP_CAPTURE"] = str(capture_path)

            result = run(
                [
                    PWSH,
                    "-NoProfile",
                    "-File",
                    str(SCRIPTS / "pull_vps_application_reports.ps1"),
                    "-RemoteRepoPath",
                    "/root/Job App's",
                    "-ConfigPath",
                    str(config_path),
                    "-Destination",
                    str(destination),
                ],
                cwd=directory,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads((destination / "submission_log.json").read_text(encoding="utf-8")),
                {"confirmed": {}},
            )
            self.assertEqual(
                json.loads(
                    (destination / "vps_application_failures.json").read_text(encoding="utf-8")
                ),
                {"failure_count": 0, "failures": []},
            )
            self.assertEqual(
                json.loads((destination / "vps_infra_status.json").read_text(encoding="utf-8")),
                {"active_services": [], "uptime": "up 1 day"},
            )
            captures = [
                json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(captures), 3)
            for capture in captures:
                arguments = capture["arguments"]
                self.assertIn("-pwfile", arguments)
                self.assertIn("-hostkey", arguments)
                self.assertIn("ssh-ed25519 255 SHA256:trusted", arguments)
                self.assertNotIn("-pw", arguments)
                self.assertNotIn(password, arguments)
                self.assertEqual(capture["password_content"], password)
                self.assertFalse(Path(capture["password_path"]).exists())
            self.assertIn(
                "tester@example.test:/root/Job App's/output/submission_log.json",
                captures[0]["arguments"],
            )

            blocked = run(
                [
                    PWSH,
                    "-NoProfile",
                    "-File",
                    str(SCRIPTS / "pull_vps_application_reports.ps1"),
                    "-ConfigPath",
                    str(config_path),
                    "-Destination",
                    str(destination),
                ],
                cwd=directory,
                env=environment,
            )
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("Use -Overwrite", blocked.stderr)
            self.assertEqual(len(capture_path.read_text(encoding="utf-8").splitlines()), 3)
