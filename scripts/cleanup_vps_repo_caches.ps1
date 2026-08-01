# Removes regenerable cache and editor-temporary files from the VPS checkout.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

try {
    $Connection = Read-VpsConnectionConfig -Path $ConfigPath
    $PlinkPath = Get-RequiredCommandPath -Name "plink"
} catch {
    Write-Error $_
    exit 1
}

$RemoteRepoPath = $RemoteRepoPath.TrimEnd("/")
if (-not $RemoteRepoPath.StartsWith("/") -or $RemoteRepoPath -match "\s") {
    Write-Error "RemoteRepoPath must be an absolute POSIX path without whitespace."
    exit 1
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$RemoteCommand = @"
set -eu
repo=$Repo
resolved=`$(readlink -f "`$repo")
if [ "`$resolved" != "`$repo" ] || [ ! -d "`$repo/.git" ]; then
  printf '%s\n' "Refusing cleanup for unexpected repository path: `$resolved" >&2
  exit 64
fi
python3 - "`$repo" <<'PY'
import json
import shutil
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
excluded = {".git", ".venv", "config", "output"}
cache_names = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
temp_suffixes = {".pyc", ".pyo", ".tmp", ".swp", ".swo"}

directories = []
files = []
for path in repo.rglob("*"):
    relative = path.relative_to(repo)
    if relative.parts and relative.parts[0] in excluded:
        continue
    if path.is_dir() and path.name in cache_names:
        directories.append(path)
    elif path.is_file() and (path.suffix.casefold() in temp_suffixes or path.name.endswith("~")):
        files.append(path)

bytes_deleted = sum(path.stat().st_size for path in files)
for directory in directories:
    if directory.exists():
        bytes_deleted += sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())
        shutil.rmtree(directory)
for path in files:
    if path.exists():
        path.unlink()

remaining = []
for path in repo.rglob("*"):
    relative = path.relative_to(repo)
    if relative.parts and relative.parts[0] in excluded:
        continue
    if path.is_dir() and path.name in cache_names:
        remaining.append(str(relative))
    elif path.is_file() and (path.suffix.casefold() in temp_suffixes or path.name.endswith("~")):
        remaining.append(str(relative))

print(json.dumps({
    "cache_directories_deleted": len(directories),
    "temporary_files_deleted": len(files),
    "bytes_deleted": bytes_deleted,
    "remaining": len(remaining),
}, sort_keys=True))
if remaining:
    raise SystemExit("cache cleanup verification failed: " + ", ".join(remaining[:10]))
PY
"@

$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "vps-repo-cache-cleanup"
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
    $ExitCode = $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

if ($ExitCode -ne 0) {
    Write-Error "VPS repository cache cleanup failed with exit code $ExitCode."
}
exit $ExitCode
