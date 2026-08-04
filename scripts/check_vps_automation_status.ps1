# Reads the current VPS automation process and recent log without starting work.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 500)]
    [int]$LogLines = 80,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

if (-not $RemoteRepoPath.StartsWith("/")) {
    Write-Error "RemoteRepoPath must be an absolute POSIX path."
    exit 1
}
try {
    $Connection = Read-VpsConnectionConfig -Path $ConfigPath
    $PlinkPath = Get-RequiredCommandPath -Name "plink"
} catch {
    Write-Error $_
    exit 1
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$RemoteCommand = @"
set -eu
repo=$Repo
printf '%s\n' '=== VPS CLOCK AND UPTIME ==='
date --iso-8601=seconds
uptime
printf '%s\n' '=== CONTINUOUS SERVICES ==='
systemctl show job-app-greenhouse.service \
  --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp \
  2>/dev/null || true
printf '%s\n' '=== AUTOMATION PROCESSES ==='
pgrep -af '[c]ontinuous-greenhouse|[j]ob_automation.py apply' |
  sed -E 's/(--email )[[:graph:]]+/\1[REDACTED]/g' || true
printf '%s\n' '=== REPOSITORY STATE ==='
git -C "`$repo" status --short --branch
git -C "`$repo" log -1 --date=iso-strict --pretty=format:'%H|%ad|%s'
printf '\n'
printf '%s\n' '=== OUTPUT FILES ==='
for name in vps_document_archive_state.json submission_log.json vps_application_failures.json \
  vps_application_state.json continuous_greenhouse_state.json; do
  if [ -f "`$repo/output/`$name" ]; then
    stat -c '%n|%s bytes|%y' "`$repo/output/`$name"
  else
    printf '%s\n' "`$repo/output/`$name|MISSING"
  fi
done
printf '%s\n' '=== CONTINUOUS GREENHOUSE SUMMARY ==='
python3 - "`$repo/output/continuous_greenhouse_state.json" <<'PY'
import json
import re
import sys
from collections import Counter
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    print("MISSING")
else:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = [
        value for value in payload.get("jobs", {}).values()
        if isinstance(value, dict)
    ]
    print("status_counts=" + json.dumps(Counter(
        str(record.get("status", "unknown")) for record in records
    ), sort_keys=True))
    if records:
        latest = max(records, key=lambda record: str(record.get("updated_at", "")))
        summary = {
            key: latest.get(key)
            for key in (
                "status",
                "stage",
                "company",
                "title",
                "result_status",
                "exit_code",
                "timed_out",
                "resume_valid",
                "cover_letter_valid",
                "ledger_confirmed",
                "updated_at",
            )
            if key in latest
        }
        print("latest=" + json.dumps(summary, sort_keys=True))
        diagnostic = str(latest.get("stderr_tail") or latest.get("stdout_tail") or "")
        diagnostic = re.sub(
            r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
            "[REDACTED_EMAIL]",
            diagnostic,
        )
        nonempty = [line for line in diagnostic.splitlines() if line.strip()]
        if nonempty:
            print("latest_diagnostic_tail:")
            print("\n".join(nonempty[-12:]))
PY
printf '%s\n' '=== RECENT LOG ==='
printf '%s\n' '=== RECENT CONTINUOUS GREENHOUSE JOURNAL ==='
journalctl -u job-app-greenhouse.service -n $LogLines --no-pager 2>/dev/null || true
"@

$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "vps-status"
try {
    $PlinkArguments = @(
        "-ssh",
        "-batch",
        "-P",
        $Connection.Port,
        "-hostkey",
        $Connection.HostKey,
        "-pwfile",
        $PasswordFile,
        "$($Connection.User)@$($Connection.Host)",
        $RemoteCommand
    )
    $Execution = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkPath `
        -ArgumentList $PlinkArguments `
        -TimeoutSeconds $TimeoutSeconds
    foreach ($OutputLine in $Execution.Output) {
        Write-Output ([string]$OutputLine)
    }
    if ($Execution.TimedOut) {
        Write-Error "VPS status check timed out after $TimeoutSeconds seconds."
    }
    $RemoteExitCode = $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

exit $RemoteExitCode
