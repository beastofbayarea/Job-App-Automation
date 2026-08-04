# Compatibility entry point for the fleet-aware, read-only ATS status report.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$LogLines = 80,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
)

$StatusScript = Join-Path $PSScriptRoot "check_vps_parallel_ats.ps1"
& $StatusScript @PSBoundParameters
exit $LASTEXITCODE
