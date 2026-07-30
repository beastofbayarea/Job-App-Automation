# Reads every continuous ATS service without starting or stopping work.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$LogLines = 60,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
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

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
if (-not $PlinkCmd) {
    Write-Error "plink.exe must be available on PATH."
    exit 1
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$RemoteCommand = @"
set -eu
repo=$Repo
printf '%s\n' '=== VPS CAPACITY ==='
date --iso-8601=seconds
uptime
free -h
printf '%s\n' '=== PARALLEL ATS SERVICES ==='
ats_services=`$(systemctl list-unit-files 'job-app-*.service' --no-legend --no-pager |
  awk '{print `$1}' |
  while read -r service; do
    exec_start=`$(systemctl show "`$service" --property=ExecStart --value 2>/dev/null || true)
    case "`$exec_start" in
      *continuous_ats*|*continuous-ashby*|*continuous-greenhouse*|*continuous-lever*)
        printf '%s\n' "`$service"
        ;;
    esac
  done)
if [ -n "`$ats_services" ]; then
  systemctl show `$ats_services \
    --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp \
    2>/dev/null || true
else
  printf '%s\n' 'No continuous ATS services installed.'
fi
printf '%s\n' '=== ATS PROCESSES ==='
pgrep -af '[c]ontinuous-(ashby|greenhouse|lever)|[c]ontinuous_ats|[j]ob_automation.py apply' |
  sed -E 's/(--email )[[:graph:]]+/\1[REDACTED]/g' || true
printf '%s\n' '=== PROVIDER STATE ==='
python3 - "`$repo"/output/continuous_*_state.json <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

for raw_path in sys.argv[1:]:
    path = Path(raw_path)
    provider = path.name.removeprefix("continuous_").removesuffix("_state.json")
    if not path.is_file():
        print(f"{provider}: MISSING")
        continue
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = [value for value in payload.get("jobs", {}).values() if isinstance(value, dict)]
    counts = Counter(str(record.get("status", "unknown")) for record in records)
    print(f"{provider}: status_counts=" + json.dumps(counts, sort_keys=True))
    if records:
        latest = max(records, key=lambda record: str(record.get("updated_at", "")))
        summary = {
            key: latest.get(key)
            for key in (
                "status",
                "stage",
                "company",
                "title",
                "result_status",
                "ledger_confirmed",
                "updated_at",
            )
            if key in latest
        }
        print(f"{provider}: latest=" + json.dumps(summary, sort_keys=True))
PY
for service in `$ats_services; do
  printf '=== %s JOURNAL ===\n' "`$service"
  journalctl -u "`$service" -n $LogLines --no-pager 2>/dev/null || true
done
"@

$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "vps-parallel-ats-$([guid]::NewGuid().ToString('N')).txt"
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
        Write-Error "VPS parallel ATS status check timed out after $TimeoutSeconds seconds."
    }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
