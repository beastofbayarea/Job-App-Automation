param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "tracker-retry-inspect"
$RemoteCommand = @"
set -eu
repo=$Repo
latest=`$(find "`$repo/output" -maxdepth 1 -type d -name 'greenhouse-tracker-retry.*' -printf '%T@|%p\n' | sort -nr | head -n 1 | cut -d'|' -f2-)
test -n "`$latest"
test -f "`$latest/state.json"
python3 - "`$latest/state.json" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
records = [item for item in payload.get("jobs", {}).values() if isinstance(item, dict)]
if len(records) != 1:
    raise SystemExit(f"expected one retry record, found {len(records)}")
record = records[0]
result = record.get("result") if isinstance(record.get("result"), dict) else {}
print(json.dumps({
    "retry_directory": path.parent.name,
    "company": record.get("company"),
    "title": record.get("title"),
    "status": record.get("status"),
    "result_status": record.get("result_status"),
    "exit_code": record.get("exit_code"),
    "timed_out": record.get("timed_out"),
    "submitted": result.get("submitted"),
    "confirmed": result.get("confirmed"),
    "error": result.get("error") or result.get("detail"),
    "missing_required": result.get("missing_required", []),
}, ensure_ascii=False, sort_keys=True))
for name in ("stdout_tail", "stderr_tail"):
    text = str(record.get(name, "")).strip()
    text = re.sub(r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", "[REDACTED_EMAIL]", text)
    if text:
        print(f"{name}={text[-4000:]}")
PY
"@
$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)", $RemoteCommand
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { exit $Execution.ExitCode }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
