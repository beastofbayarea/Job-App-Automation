# Restarts repository-backed VPS services and verifies their deployed runtime.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 180
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$RemoteCommand = @"
set -eu
cd $Repo
git diff --quiet
git diff --cached --quiet
test ! -e config/runtime_config.json
.venv/bin/python - <<'PY'
from job_application_automation.core.runtime_config import (
    RUNTIME_CONFIG,
    RUNTIME_CONFIG_DIR,
    RUNTIME_SECTION_NAMES,
)
assert RUNTIME_CONFIG_DIR.is_dir()
actual = {path.name for path in RUNTIME_CONFIG_DIR.glob("*.json")}
expected = {"schema_version.json", *(f"{name}.json" for name in RUNTIME_SECTION_NAMES)}
assert actual == expected, (actual, expected)
assert RUNTIME_CONFIG.application["tracker_file"]
print("RUNTIME_CONFIG_OK")
PY
units=`$(
  {
    systemctl list-unit-files 'job-app-*.service' --no-legend --no-pager
    systemctl list-unit-files 'vps-dashboard.service' --no-legend --no-pager
  } 2>/dev/null | awk '`$2 == "enabled" { print `$1 }'
)
test -n "`$units"
systemctl restart `$units
for unit in `$units; do
  systemctl is-active --quiet "`$unit"
done
systemctl show `$units --property=Id,ActiveState,SubState,MainPID,NRestarts --no-pager
git rev-parse --short=7 HEAD
git status --porcelain
"@
$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand

$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "vps-runtime-restart"
try {
    $Execution = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkPath `
        -ArgumentList @(
            "-ssh", "-batch", "-P", $Connection.Port,
            "-hostkey", $Connection.HostKey,
            "-pwfile", $PasswordFile,
            "$($Connection.User)@$($Connection.Host)",
            $RemoteCommand
        ) `
        -TimeoutSeconds $TimeoutSeconds
    foreach ($OutputLine in $Execution.Output) {
        Write-Output ([string]$OutputLine)
    }
    if ($Execution.TimedOut) {
        throw "VPS runtime restart timed out after $TimeoutSeconds seconds."
    }
    exit $Execution.ExitCode
} finally {
    if (Test-Path -LiteralPath $PasswordFile) {
        Remove-Item -LiteralPath $PasswordFile -Force
    }
}
