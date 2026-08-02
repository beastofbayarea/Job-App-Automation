param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-inspect"
$RemoteCommand = @"
set -eu
repo=$Repo
systemctl show 'job-app-greenhouse-failed-*.service' --property=Id,ActiveState,SubState,NRestarts,ExecMainStatus
python3 - "`$repo/output" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in sorted(root.glob("continuous_greenhouse_failed_*_state.json")):
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = list(payload.get("jobs", {}).values())
    latest = records[-1] if records else {}
    result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
    print(json.dumps({
        "worker": path.stem,
        "records": len(records),
        "company": latest.get("company"),
        "title": latest.get("title"),
        "status": latest.get("status"),
        "result_status": latest.get("result_status"),
        "missing_required": result.get("missing_required", []),
        "detail": result.get("detail") or result.get("error"),
    }, ensure_ascii=False, sort_keys=True))
PY
"@

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
