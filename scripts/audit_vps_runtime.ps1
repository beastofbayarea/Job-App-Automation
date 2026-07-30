# Produces a read-only inventory of persistent and scheduled VPS workloads.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(5, 200)]
    [int]$ProcessLimit = 40,
    [ValidateRange(1, 200)]
    [int]$LogLines = 40,
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

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$RemoteCommand = @"
set -u
repo=$Repo
redact() {
  sed -E \
    -e 's/(--email )[[:graph:]]+/\1[REDACTED]/g' \
    -e 's/[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}/[REDACTED_EMAIL]/g'
}
section() {
  printf '\n=== %s ===\n' "`$1"
}

section HOST
date --iso-8601=seconds
hostnamectl
uptime

section CAPACITY
printf 'CPU_COUNT='
nproc
free -h
swapon --show
findmnt -no SOURCE,FSTYPE,SIZE,USED,AVAIL,USE% /
df -hT / /var /root 2>/dev/null || true

section FAILED_UNITS
systemctl --failed --no-pager --plain

section RUNNING_SERVICES
systemctl list-units --type=service --state=running --no-pager --plain --all

section ENABLED_SERVICES
systemctl list-unit-files --type=service --state=enabled --no-pager --plain

section TIMERS
systemctl list-timers --all --no-pager --plain

section ROOT_CRONTAB
crontab -l 2>&1 | redact || true

section USER_CRONTABS
for cron_file in /var/spool/cron/crontabs/*; do
  [ -f "`$cron_file" ] || continue
  printf '%s\n' "--- `$cron_file"
  grep -vE '^[[:space:]]*(#|`$)' "`$cron_file" | redact || true
done

section SYSTEM_CRON_FILES
find /etc/cron.d /etc/cron.daily /etc/cron.hourly /etc/cron.weekly /etc/cron.monthly \
  -maxdepth 1 -type f -printf '%p\n' 2>/dev/null | sort

section LONGEST_LIVED_PROCESSES
ps -eo user,pid,ppid,etimes,%cpu,%mem,rss,stat,comm,args --sort=-etimes |
  awk 'NR == 1 || `$3 != 2' |
  grep -v '[b]ash -c set -u repo=' |
  head -n $ProcessLimit |
  redact

section TOP_CPU_PROCESSES
ps -eo user,pid,ppid,etimes,%cpu,%mem,rss,stat,comm,args --sort=-%cpu |
  awk 'NR == 1 || `$3 != 2' |
  grep -v '[b]ash -c set -u repo=' |
  head -n $ProcessLimit |
  redact

section TOP_MEMORY_PROCESSES
ps -eo user,pid,ppid,etimes,%cpu,%mem,rss,stat,comm,args --sort=-rss |
  awk 'NR == 1 || `$3 != 2' |
  grep -v '[b]ash -c set -u repo=' |
  head -n $ProcessLimit |
  redact

section LISTENING_SOCKETS
ss -lntup

section CONTAINERS
if command -v docker >/dev/null 2>&1; then
  docker ps --no-trunc
  docker system df
else
  printf '%s\n' 'docker: not installed'
fi
if command -v podman >/dev/null 2>&1; then
  podman ps --no-trunc
else
  printf '%s\n' 'podman: not installed'
fi

section JOB_APP_UNITS
job_app_units=`$(systemctl list-unit-files 'job-app-*.service' --no-legend --no-pager |
  awk '{print `$1}')
if [ -z "`$job_app_units" ]; then
  printf '%s\n' 'No job-app services installed.'
else
  systemctl show `$job_app_units --no-pager \
    --property=Id,Description,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,CPUUsageNSec,MemoryCurrent,MemoryPeak,TasksCurrent,TasksMax,ExecStart
fi

section SELECTED_APPLICATION_UNITS
for unit in nginx.service cent-capital-backend.service vps-dashboard.service docker.service containerd.service; do
  systemctl show "`$unit" --no-pager \
    --property=Id,Description,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp,CPUUsageNSec,MemoryCurrent,MemoryPeak,TasksCurrent,ExecStart \
    2>/dev/null || true
done

section APPLICATION_SERVICE_DIAGNOSTICS
for unit in nginx.service cent-capital-backend.service vps-dashboard.service; do
  printf '%s\n' "--- `$unit status"
  systemctl status "`$unit" --no-pager --full | sed -n '1,30p' || true
  printf '%s\n' "--- `$unit definition"
  systemctl cat "`$unit" --no-pager 2>/dev/null | redact || true
  printf '%s\n' "--- `$unit journal"
  journalctl -u "`$unit" -n $LogLines --no-pager 2>/dev/null | redact || true
done
printf '%s\n' '--- nginx configuration test'
nginx -t 2>&1 || true
printf '%s\n' '--- nginx virtual-host routing'
nginx -T 2>/dev/null |
  grep -E '^[[:space:]]*(listen|server_name|root|location|proxy_pass|ssl_certificate)' || true

section JOURNAL_AND_LARGE_LOGS
journalctl --disk-usage
find /var/log "`$repo/output" -xdev -type f -printf '%s %p\n' 2>/dev/null |
  sort -nr |
  head -n 30

section REBOOT_AND_UPDATE_STATUS
last -x reboot shutdown | head -n 15
if [ -f /var/run/reboot-required ]; then
  printf '%s\n' 'REBOOT_REQUIRED=yes'
  cat /var/run/reboot-required.pkgs 2>/dev/null || true
else
  printf '%s\n' 'REBOOT_REQUIRED=no'
fi
if command -v pro >/dev/null 2>&1; then
  pro security-status 2>/dev/null | sed -n '1,30p' || true
fi
"@

$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "vps-runtime-audit-$([guid]::NewGuid().ToString('N')).txt"
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
        Write-Error "VPS runtime audit timed out after $TimeoutSeconds seconds."
    }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
