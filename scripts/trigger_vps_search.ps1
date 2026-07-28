# scripts/trigger_vps_search.ps1
# Runs an out-of-cycle VPS search instead of waiting for the daily cron, then
# pulls the fresh output locally on success.
param(
    [Parameter(Mandatory = $true)]
    [string]$RemoteRepoPath,
    [string]$ConfigPath = "config/vps_config.json"
)

if (-not (Test-Path $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
if (-not $PlinkCmd) {
    Write-Error "plink.exe not found on PATH. Install PuTTY or add it to PATH."
    exit 1
}

$Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$VpsHost = $Config.vps.host
$SshUser = $Config.vps.ssh_user
$SshPassword = $Config.vps.ssh_password.value

if (-not $VpsHost -or -not $SshUser -or -not $SshPassword) {
    Write-Error "$ConfigPath is missing vps.host, vps.ssh_user, or vps.ssh_password.value"
    exit 1
}

$RemoteCommand = "bash $RemoteRepoPath/scripts/vps_search_sync.sh"
Write-Host "Running remote search on $VpsHost..."

& plink -ssh -batch -pw $SshPassword "$SshUser@$VpsHost" $RemoteCommand
$RemoteExitCode = $LASTEXITCODE

if ($RemoteExitCode -ne 0) {
    Write-Error "Remote search failed (exit code $RemoteExitCode). Not pulling output."
    exit $RemoteExitCode
}

Write-Host "Remote search finished. Pulling output locally..."
& pwsh "$PSScriptRoot\pull_search_output.ps1"
exit $LASTEXITCODE
