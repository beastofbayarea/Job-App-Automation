# Stops and disables only the continuous VPS job-search service.
param(
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile `
    -Password $Connection.Password `
    -Prefix "vps-stop-search"
$RemoteCommand = @'
set -eu
unit=job-app-search-sync.service
printf '%s\n' '=== BEFORE ==='
systemctl show "$unit" \
  --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts
systemctl disable "$unit"
systemctl stop --no-block "$unit"
attempts=0
while systemctl is-active --quiet "$unit" && [ "$attempts" -lt 20 ]; do
  sleep 1
  attempts=$((attempts + 1))
done
if systemctl is-active --quiet "$unit"; then
  systemctl kill --kill-who=all --signal=SIGTERM "$unit" 2>/dev/null || true
  sleep 3
fi
if systemctl is-active --quiet "$unit"; then
  systemctl kill --kill-who=all --signal=SIGKILL "$unit" 2>/dev/null || true
  systemctl stop --no-block "$unit" 2>/dev/null || true
fi
systemctl reset-failed "$unit" 2>/dev/null || true
printf '%s\n' '=== AFTER ==='
systemctl show "$unit" \
  --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts
if systemctl is-active --quiet "$unit"; then
  printf '%s\n' 'SEARCH_SERVICE_STILL_ACTIVE'
  exit 1
fi
if pgrep -af '[v]ps_continuous_search_sync|[v]ps_search_sync.sh|[s]earch_applications|[s]earch_documents'; then
  printf '%s\n' 'SEARCH_PROCESS_STILL_PRESENT'
  exit 1
fi
printf '%s\n' 'JOB_SEARCH_SERVICE_STOPPED_AND_DISABLED'
'@

try {
    $Execution = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkPath `
        -ArgumentList @(
            "-ssh", "-batch", "-P", $Connection.Port,
            "-hostkey", $Connection.HostKey,
            "-pwfile", $PasswordFile,
            "$($Connection.User)@$($Connection.Host)",
            (ConvertTo-LfLineEndings $RemoteCommand)
        ) `
        -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
