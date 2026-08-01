# Performs conservative VPS housekeeping while preserving application evidence.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 3650)] [int]$GeneratedDocumentRetentionDays = 14,
    [ValidateRange(32, 2048)] [int]$JournalRetentionMB = 100,
    [ValidateRange(1, 300)] [int]$TimeoutSeconds = 120
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
expected_output="`$repo/output"
resolved_repo=`$(readlink -f "`$repo")
resolved_output=`$(readlink -f "`$expected_output")
if [ "`$resolved_repo" != "`$repo" ] || [ ! -d "`$repo/.git" ]; then
  printf '%s\n' "Refusing maintenance for unexpected repository path: `$resolved_repo" >&2
  exit 64
fi
if [ "`$resolved_output" != "`$expected_output" ] || [ ! -d "`$resolved_output" ]; then
  printf '%s\n' "Refusing maintenance for unexpected output path: `$resolved_output" >&2
  exit 64
fi
if pgrep -f '[j]ob_automation.py apply' >/dev/null; then
  printf '%s\n' 'An application is active; refusing maintenance cleanup.' >&2
  exit 76
fi

printf '%s\n' '=== BEFORE ==='
df -B1 / | tail -n 1
journalctl --disk-usage

python3 - "`$resolved_output" "$GeneratedDocumentRetentionDays" <<'PY'
import json
import sys
import time
from pathlib import Path

output = Path(sys.argv[1]).resolve()
cutoff = time.time() - int(sys.argv[2]) * 86400
suffixes = ("_Resume.pdf", "_Cover_Letter.pdf")
targets = [path for path in output.iterdir() if path.is_file()
           and path.name.endswith(suffixes) and path.stat().st_mtime < cutoff]
bytes_deleted = sum(path.stat().st_size for path in targets)
for path in targets:
    path.unlink()
print(json.dumps({"generated_documents_deleted": len(targets),
                  "generated_document_bytes_deleted": bytes_deleted,
                  "retention_days": int(sys.argv[2])}, sort_keys=True))
PY

python3 - <<'PY'
import json
import time
from pathlib import Path

cutoff = time.time() - 2 * 86400
suffixes = {".tar", ".tgz", ".gz", ".zip"}
targets = [path for path in Path("/tmp").iterdir() if path.is_file()
           and "job-app" in path.name.casefold()
           and path.suffix.casefold() in suffixes and path.stat().st_mtime < cutoff]
bytes_deleted = sum(path.stat().st_size for path in targets)
for path in targets:
    path.unlink()
print(json.dumps({"temporary_bundles_deleted": len(targets),
                  "temporary_bundle_bytes_deleted": bytes_deleted}, sort_keys=True))
PY

journalctl --vacuum-size=${JournalRetentionMB}M
logrotate /etc/logrotate.conf
apt-get clean
git -C "`$repo" gc --prune=30.days --quiet

printf '%s\n' '=== AFTER ==='
df -B1 / | tail -n 1
journalctl --disk-usage
git -C "`$repo" status --short
systemctl is-active vps-dashboard.service
nginx -t
"@

$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "vps-maintenance-cleanup"
try {
    $Execution = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkPath `
        -ArgumentList @(
            "-ssh", "-batch", "-P", $Connection.Port,
            "-hostkey", $Connection.HostKey, "-pwfile", $PasswordFile,
            "$($Connection.User)@$($Connection.Host)",
            (ConvertTo-LfLineEndings $RemoteCommand)
        ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    $ExitCode = $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

if ($ExitCode -eq 124) {
    Write-Error "VPS maintenance cleanup timed out after $TimeoutSeconds seconds."
} elseif ($ExitCode -ne 0) {
    Write-Error "VPS maintenance cleanup failed with exit code $ExitCode."
}
exit $ExitCode
