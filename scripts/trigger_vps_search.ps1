# scripts/trigger_vps_search.ps1
# Runs an out-of-cycle VPS search instead of waiting for the daily cron, then
# pulls the fresh output locally on success.
param(
    [Parameter(Mandatory = $true)]
    [string]$RemoteRepoPath,
    [string]$ConfigPath = "config/vps_config.json"
)

. "$PSScriptRoot\vps_script_helpers.ps1"

if (-not (Test-Path $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}

$RemoteRepoPath = $RemoteRepoPath.TrimEnd("/")
if (-not $RemoteRepoPath.StartsWith("/")) {
    Write-Error "RemoteRepoPath must be an absolute POSIX path."
    exit 1
}

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
if (-not $PlinkCmd) {
    Write-Error "plink.exe not found on PATH. Install PuTTY or add it to PATH."
    exit 1
}

try {
    $Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
} catch {
    Write-Error "VPS config at $ConfigPath is not valid JSON."
    exit 1
}

$VpsHost = $Config.vps.host
$SshUser = $Config.vps.ssh_user
$SshPassword = $Config.vps.ssh_password.value
$SshHostKey = $Config.vps.ssh_host_key
$SshPort = if ($null -ne $Config.vps.ssh_port) { [int]$Config.vps.ssh_port } else { 22 }

if (-not $VpsHost -or -not $SshUser -or -not $SshPassword -or -not $SshHostKey) {
    Write-Error "$ConfigPath is missing vps.host, vps.ssh_user, vps.ssh_password.value, or vps.ssh_host_key"
    exit 1
}
if ($SshPort -lt 1 -or $SshPort -gt 65535) {
    Write-Error "$ConfigPath contains an invalid vps.ssh_port"
    exit 1
}

$RemoteScriptPath = "$RemoteRepoPath/scripts/vps_search_sync.sh"
$RemoteCommand = "exec bash -- $(ConvertTo-PosixShellLiteral $RemoteScriptPath)"
Write-Host "Running remote search on $VpsHost..."

$PasswordFile = Join-Path ([System.IO.Path]::GetTempPath()) "job-app-plink-$([guid]::NewGuid().ToString('N')).txt"
try {
    [System.IO.File]::WriteAllText(
        $PasswordFile,
        [string]$SshPassword,
        [System.Text.UTF8Encoding]::new($false)
    )
    & $PlinkCmd.Source -ssh -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile "$SshUser@$VpsHost" $RemoteCommand
    $RemoteExitCode = $LASTEXITCODE
} finally {
    if (Test-Path -LiteralPath $PasswordFile) {
        Remove-Item -LiteralPath $PasswordFile -Force
    }
}

if ($RemoteExitCode -ne 0) {
    Write-Error "Remote search failed (exit code $RemoteExitCode). Not pulling output."
    exit $RemoteExitCode
}

Write-Host "Remote search finished. Pulling output locally..."
& pwsh "$PSScriptRoot\pull_search_output.ps1"
exit $LASTEXITCODE
