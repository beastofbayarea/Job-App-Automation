param(
    [ValidatePattern("^\d+$")]
    [string]$JobId,
    [switch]$InspectLatest,
    [string]$ScreenshotOutputPath = "",
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json"
)

. "$PSScriptRoot\vps_script_helpers.ps1"

if (-not $InspectLatest -and -not $JobId) {
    Write-Error "JobId is required unless -InspectLatest is used."
    exit 1
}
if ($InspectLatest -and $JobId) {
    Write-Error "Use either -JobId or -InspectLatest, not both."
    exit 1
}
if ($ScreenshotOutputPath -and -not $InspectLatest) {
    Write-Error "ScreenshotOutputPath requires -InspectLatest."
    exit 1
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}
$RemoteRepoPath = $RemoteRepoPath.TrimEnd("/")
if (
    -not $RemoteRepoPath.StartsWith("/") -or
    $RemoteRepoPath -match "\s" -or
    $RemoteRepoPath.Contains([char]0)
) {
    Write-Error "RemoteRepoPath must be an absolute POSIX path without whitespace."
    exit 1
}

try {
    $Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
} catch {
    Write-Error "VPS config at $ConfigPath is not valid JSON."
    exit 1
}

$VpsHost = [string]$Config.vps.host
$SshUser = [string]$Config.vps.ssh_user
$SshPassword = [string]$Config.vps.ssh_password.value
$SshHostKey = [string]$Config.vps.ssh_host_key
$SshPort = if ($null -ne $Config.vps.ssh_port) { [int]$Config.vps.ssh_port } else { 22 }
if (-not $VpsHost -or -not $SshUser -or -not $SshPassword -or -not $SshHostKey) {
    Write-Error "$ConfigPath is missing required pinned VPS connection settings."
    exit 1
}
if ($SshPort -lt 1 -or $SshPort -gt 65535) {
    Write-Error "$ConfigPath contains an invalid vps.ssh_port."
    exit 1
}

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
$PscpCmd = Get-Command pscp -ErrorAction SilentlyContinue
if (-not $PlinkCmd -or ($ScreenshotOutputPath -and -not $PscpCmd)) {
    Write-Error "plink.exe is required; pscp.exe is also required for screenshot downloads."
    exit 1
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$JobIdLiteral = ConvertTo-PosixShellLiteral ([string]$JobId)
$PasswordFile = Join-Path (
    [IO.Path]::GetTempPath()
) "greenhouse-retry-$([guid]::NewGuid().ToString("N")).txt"
$RemoteCommand = if ($InspectLatest) {
    @"
set -eu
repo=$Repo
latest=`$(find "`$repo/output" -maxdepth 1 -type d \
  -name 'greenhouse-targeted-retry.*' -printf '%T@|%p\n' |
  sort -nr | head -n 1 | cut -d'|' -f2-)
if [ -z "`$latest" ] || [ ! -f "`$latest/state.json" ]; then
  printf '%s\n' 'No targeted Greenhouse retry state was found.' >&2
  exit 1
fi
python3 - "`$latest/state.json" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
records = [
    record for record in payload.get("jobs", {}).values()
    if isinstance(record, dict)
]
if len(records) != 1:
    raise SystemExit(f"expected one retry record, found {len(records)}")
record = records[0]
result = record.get("result") if isinstance(record.get("result"), dict) else {}
summary = {
    "retry_directory": path.parent.name,
    "status": record.get("status"),
    "stage": record.get("stage"),
    "result_status": record.get("result_status"),
    "exit_code": record.get("exit_code"),
    "timed_out": record.get("timed_out"),
    "submitted": result.get("submitted"),
    "confirmed": result.get("confirmed"),
    "engine_status": result.get("status"),
    "error": result.get("error") or result.get("detail"),
    "missing_required": result.get("missing_required", []),
}
print(json.dumps(summary, sort_keys=True, ensure_ascii=False))
diagnostic_lines = []
for name in ("stdout_tail", "stderr_tail"):
    text = str(record.get(name, "")).strip()
    text = re.sub(
        r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
        "[REDACTED_EMAIL]",
        text,
    )
    diagnostic_lines.extend(
        line
        for line in text.splitlines()
        if re.search(
            r"security code|verification|gmail|required field|screenshot",
            line,
            flags=re.IGNORECASE,
        )
    )
    if text:
        print(f"{name}={text[-2000:]}")
if diagnostic_lines:
    print("selected_diagnostics=" + "\n".join(diagnostic_lines[-20:]))
PY
latest_screenshot=`$(find "`$repo/output" -maxdepth 1 -type f \
  -iname '*ai71*prefilled*.png' -printf '%T@|%p\n' |
  sort -nr | head -n 1 | cut -d'|' -f2-)
if [ -n "`$latest_screenshot" ]; then
  printf 'latest_prefilled_screenshot=%s\n' "`$latest_screenshot"
fi
systemctl is-active job-app-greenhouse.service
"@
} else {
    @"
set -eu
repo=$Repo
target_id=$JobIdLiteral
if pgrep -f '[j]ob_automation.py (apply|documents generate)' >/dev/null; then
  printf '%s\n' 'An application or document-generation process is active; refusing overlap.' >&2
  exit 76
fi
retry_root=`$(mktemp -d "`$repo/output/greenhouse-targeted-retry.XXXXXXXX")
python3 - \
  "`$repo/output/continuous_greenhouse_state.json" \
  "`$repo/output/continuous_greenhouse_jobs.json" \
  "`$repo/output/vps_generation_jobs.json" \
  "`$repo/output/submission_log.json" \
  "`$retry_root/jobs.json" \
  "`$target_id" <<'PY'
import json
import sys
from pathlib import Path

state_path, provider_input, shared_input, ledger_path, retry_input = map(
    Path, sys.argv[1:6]
)
target_id = sys.argv[6]
state = json.loads(state_path.read_text(encoding="utf-8"))
prior = [
    record
    for record in state.get("jobs", {}).values()
    if isinstance(record, dict) and target_id in str(record.get("job_url", ""))
]
if len(prior) != 1:
    raise SystemExit(f"expected exactly one prior target record, found {len(prior)}")
record = prior[0]
result = record.get("result") if isinstance(record.get("result"), dict) else {}
if (
    record.get("status") != "failed"
    or result.get("submitted") is not False
    or result.get("confirmed") is True
):
    raise SystemExit(
        "refusing retry: prior target is not a verified pre-submit failure"
    )

ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
ledger_records = ledger.values() if isinstance(ledger, dict) else ledger
if any(
    isinstance(item, dict)
    and target_id in str(item.get("job_url", ""))
    and item.get("status") == "SUBMITTED & CONFIRMED"
    for item in ledger_records
):
    raise SystemExit("refusing retry: target already exists in the confirmed ledger")

selected = None
for candidate_path in (provider_input, shared_input):
    if not candidate_path.is_file():
        continue
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    for job in payload if isinstance(payload, list) else []:
        if isinstance(job, dict) and target_id in str(job.get("job_url", "")):
            selected = job
            break
    if selected is not None:
        break
if selected is None:
    raise SystemExit("target job is absent from current verified input")

retry_input.write_text(
    json.dumps([selected], indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
print("retry_safety=verified_pre_submit_failure")
print(f"retry_company={record.get('company', '')}")
print(f"retry_title={record.get('title', '')}")
PY

systemctl stop job-app-greenhouse.service
restart_worker() {
  systemctl start job-app-greenhouse.service
}
trap restart_worker EXIT INT TERM
xvfb-run -a --server-args="-screen 0 1280x1024x24" \
  "`$repo/.venv/bin/python" -m job_application_automation.core.continuous_ats \
  --ats-platform greenhouse \
  --once \
  --input "`$retry_root/jobs.json" \
  --state "`$retry_root/state.json" \
  --results-dir "`$retry_root/results" \
  --documents-dir "`$retry_root/documents" \
  --profile "`$repo/config/candidate_profile_config.json" \
  --email-pool "`$repo/config/candidate_email_pool.json" \
  --launcher "`$repo/src/job_automation.py" \
  --submission-log "`$repo/output/submission_log.json"
"@
}

try {
    [IO.File]::WriteAllText(
        $PasswordFile,
        $SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    if ($InspectLatest) {
        $RemoteOutput = @(
            & $PlinkCmd.Source -ssh -batch -P $SshPort -hostkey $SshHostKey `
                -pwfile $PasswordFile "$SshUser@$VpsHost" $RemoteCommand
        )
        $RemoteOutput | Write-Output
    } else {
        & $PlinkCmd.Source -ssh -batch -P $SshPort -hostkey $SshHostKey `
            -pwfile $PasswordFile "$SshUser@$VpsHost" $RemoteCommand
    }
    $RemoteExitCode = $LASTEXITCODE
    if ($RemoteExitCode -eq 0 -and $ScreenshotOutputPath) {
        $ScreenshotLine = @(
            $RemoteOutput | Where-Object { $_ -like "latest_prefilled_screenshot=*" }
        ) | Select-Object -Last 1
        $RemoteScreenshot = [string]$ScreenshotLine -replace "^[^=]+=", ""
        $ExpectedPrefix = "$RemoteRepoPath/output/"
        if (-not $RemoteScreenshot.StartsWith($ExpectedPrefix)) {
            Write-Error "The inspected screenshot path was missing or outside VPS output."
            exit 1
        }
        $ResolvedScreenshotOutput = [IO.Path]::GetFullPath($ScreenshotOutputPath)
        $ScreenshotParent = Split-Path -Parent $ResolvedScreenshotOutput
        if ($ScreenshotParent) {
            [void](New-Item -ItemType Directory -Force -Path $ScreenshotParent)
        }
        & $PscpCmd.Source -batch -P $SshPort -hostkey $SshHostKey `
            -pwfile $PasswordFile `
            "$SshUser@${VpsHost}:$RemoteScreenshot" `
            $ResolvedScreenshotOutput
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Greenhouse retry screenshot download failed."
            exit $LASTEXITCODE
        }
        Write-Host "Downloaded retry screenshot to $ResolvedScreenshotOutput"
    }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

if ($RemoteExitCode -ne 0) {
    $Operation = if ($InspectLatest) { "inspection" } else { "retry" }
    Write-Error "Targeted Greenhouse $Operation failed (exit code $RemoteExitCode)."
    exit $RemoteExitCode
}
if ($InspectLatest) {
    Write-Host "Targeted Greenhouse retry inspection completed."
} else {
    Write-Host "Targeted Greenhouse retry completed with exact ledger confirmation."
}
