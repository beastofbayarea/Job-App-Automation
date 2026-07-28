# Reads the current VPS automation process and recent log without starting work.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 500)]
    [int]$LogLines = 80
)

. "$PSScriptRoot\vps_script_helpers.ps1"

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}
if (-not $RemoteRepoPath.StartsWith("/")) {
    Write-Error "RemoteRepoPath must be an absolute POSIX path."
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

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
if (-not $PlinkCmd) {
    Write-Error "plink.exe must be available on PATH."
    exit 1
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$RemoteCommand = @"
set -eu
repo=$Repo
printf '%s\n' '=== AUTOMATION PROCESSES ==='
pgrep -af 'vps_search_sync.sh|search_applications|search_documents|job_automation.py search' || true
printf '%s\n' '=== OUTPUT FILES ==='
for name in submission_log.json vps_application_failures.json vps_application_state.json vps_sync.log; do
  if [ -f "`$repo/output/`$name" ]; then
    stat -c '%n|%s bytes|%y' "`$repo/output/`$name"
  else
    printf '%s\n' "`$repo/output/`$name|MISSING"
  fi
done
printf '%s\n' '=== RECENT LOG ==='
tail -n $LogLines "`$repo/output/vps_sync.log" 2>/dev/null || true
"@

$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "vps-status-$([guid]::NewGuid().ToString('N')).txt"
try {
    [IO.File]::WriteAllText(
        $PasswordFile,
        $SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    & $PlinkCmd.Source -ssh -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile `
        "$SshUser@$VpsHost" $RemoteCommand
    $RemoteExitCode = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

exit $RemoteExitCode
