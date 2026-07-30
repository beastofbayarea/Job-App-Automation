# Stops the Cent Capital backend only when live evidence proves an auth-failure loop.
param(
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 45
)

. "$PSScriptRoot\vps_script_helpers.ps1"

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
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
if (-not $PlinkCmd) {
    Write-Error "plink.exe must be available on PATH."
    exit 1
}

$RemoteCommand = @'
set -eu
service=cent-capital-backend.service
if ! systemctl is-active --quiet "$service"; then
  printf '%s\n' 'NO_ACTION: backend is not active.'
elif ss -lnt | grep -Eq '[:.]8080[[:space:]]'; then
  printf '%s\n' 'NO_ACTION: backend is listening on port 8080.'
elif ! journalctl -u "$service" -n 500 --no-pager |
  grep -Eq 'password authentication failed|too many authentication failures'; then
  printf '%s\n' 'NO_ACTION: database authentication failure evidence is absent.'
  exit 3
else
  systemctl stop "$service"
  systemctl disable "$service"
  systemctl reset-failed "$service" || true
  printf '%s\n' 'QUARANTINED: stopped invalid-credential backend restart loop.'
fi
systemctl show "$service" --no-pager \
  --property=Id,UnitFileState,ActiveState,SubState,NRestarts
'@

$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "vps-backend-quarantine-$([guid]::NewGuid().ToString('N')).txt"
try {
    [IO.File]::WriteAllText(
        $PasswordFile,
        $SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    $Execution = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkCmd.Source `
        -ArgumentList @(
            "-ssh",
            "-batch",
            "-P",
            $SshPort,
            "-hostkey",
            $SshHostKey,
            "-pwfile",
            $PasswordFile,
            "$SshUser@$VpsHost",
            $RemoteCommand
        ) `
        -TimeoutSeconds $TimeoutSeconds
    foreach ($OutputLine in $Execution.Output) {
        Write-Output ([string]$OutputLine)
    }
    if ($Execution.TimedOut) {
        Write-Error "Backend quarantine check timed out after $TimeoutSeconds seconds."
    }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
