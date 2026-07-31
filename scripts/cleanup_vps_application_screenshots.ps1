# Deletes legacy application screenshots below the VPS output directory.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot\vps_script_helpers.ps1"

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}
$RemoteRepoPath = $RemoteRepoPath.TrimEnd("/")
if (
    -not $RemoteRepoPath.StartsWith("/") -or
    $RemoteRepoPath -match "\s" -or
    $RemoteRepoPath.Contains([char]0)
) {
    Write-Error "RemoteRepoPath must be an absolute POSIX path without whitespace."
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

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$RemoteCommand = @"
set -eu
repo=$Repo
expected="`$repo/output"
output=`$(readlink -f "`$expected")
if [ "`$output" != "`$expected" ] || [ ! -d "`$output" ]; then
  printf '%s\n' "Refusing cleanup for unexpected output path: `$output" >&2
  exit 64
fi
if pgrep -f '[j]ob_automation.py apply' >/dev/null; then
  printf '%s\n' 'An application is active; refusing screenshot cleanup.' >&2
  exit 76
fi
python3 - "`$output" "`$expected" <<'PY'
import json
import sys
from pathlib import Path

expected = Path(sys.argv[1]).resolve()
repo_output = Path(sys.argv[2]).resolve()
if expected != repo_output:
    raise SystemExit(f"refusing cleanup outside canonical output: {expected}")

extensions = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
targets = [
    path
    for path in expected.rglob("*")
    if path.is_file() and path.suffix.casefold() in extensions
]
bytes_deleted = sum(path.stat().st_size for path in targets)
for path in targets:
    path.unlink()
remaining = [
    str(path.relative_to(expected))
    for path in expected.rglob("*")
    if path.is_file() and path.suffix.casefold() in extensions
]
summary = {
    "screenshot_cleanup_path": str(expected),
    "screenshots_deleted": len(targets),
    "screenshot_bytes_deleted": bytes_deleted,
    "screenshots_remaining": len(remaining),
}
print(json.dumps(summary, sort_keys=True))
if remaining:
    raise SystemExit("screenshot cleanup verification failed: " + ", ".join(remaining[:10]))
PY
"@
$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand

$PasswordFile = Join-Path (
    [IO.Path]::GetTempPath()
) "vps-screenshot-cleanup-$([guid]::NewGuid().ToString("N")).txt"
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
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    $ExitCode = $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

if ($ExitCode -eq 124) {
    Write-Error "VPS screenshot cleanup timed out after $TimeoutSeconds seconds."
} elseif ($ExitCode -ne 0) {
    Write-Error "VPS screenshot cleanup failed with exit code $ExitCode."
}
exit $ExitCode
