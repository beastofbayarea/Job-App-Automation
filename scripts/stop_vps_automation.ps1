# Stops all Job App Automation application/search services and timers on the VPS.
param(
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 90
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "vps-stop"
$RemoteCommand = @'
set -eu
units=$(systemctl list-unit-files 'job-app-*.service' 'job-app-*.timer' --no-legend --no-pager | awk '{print $1}')
any_active() {
  for unit in $units; do
    if systemctl is-active --quiet "$unit"; then
      return 0
    fi
  done
  return 1
}
if [ -n "$units" ]; then
  systemctl stop --no-block $units
  systemctl kill --kill-who=all --signal=SIGTERM $units 2>/dev/null || true
  attempts=0
  while any_active && [ "$attempts" -lt 20 ]; do
    sleep 1
    attempts=$((attempts + 1))
  done
  if any_active; then
    systemctl kill --kill-who=all --signal=SIGKILL $units 2>/dev/null || true
    systemctl stop --no-block $units
  fi
  systemctl reset-failed $units 2>/dev/null || true
fi
systemctl list-units --all 'job-app-*.service' 'job-app-*.timer' --no-legend --no-pager
if any_active; then
  echo JOB_APP_AUTOMATION_STILL_ACTIVE
  exit 1
fi
echo JOB_APP_AUTOMATION_STOPPED
'@

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
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    exit $Execution.ExitCode
} finally {
    if (Test-Path -LiteralPath $PasswordFile) {
        Remove-Item -LiteralPath $PasswordFile -Force
    }
}
