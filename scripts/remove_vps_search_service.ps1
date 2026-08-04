# Audits or permanently removes the VPS continuous job-search service and its private runtime files.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [switch]$AuditOnly,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

if (-not $RemoteRepoPath.StartsWith("/") -or $RemoteRepoPath -match "\s") {
    throw "RemoteRepoPath must be an absolute POSIX path without whitespace."
}
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile `
    -Password $Connection.Password `
    -Prefix "vps-remove-search"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$Mode = if ($AuditOnly) { "audit" } else { "remove" }
$RemoteCommand = @"
set -eu
repo=$Repo
mode='$Mode'
unit=job-app-search-sync.service
unit_path=/etc/systemd/system/job-app-search-sync.service
deploy_key=/root/.ssh/vps_search_sync
sync_tree="`$repo/.sync-worktree"
lock_file="`$repo/.git/vps-search-sync.lock"
printf '%s\n' '=== UNIT ==='
systemctl show "`$unit" --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,FragmentPath 2>/dev/null || true
systemctl cat "`$unit" --no-pager 2>/dev/null || true
printf '%s\n' '=== RELATED FILES ==='
for path in "`$unit_path" "`$deploy_key" "`$deploy_key.pub" "`$sync_tree" "`$lock_file" \
  "`$repo/output/vps_sync.log" "`$repo/output/vps_run_status.json" \
  "`$repo/output/job_search_coverage.json" "`$repo/output/ai_jobs.csv" \
  "`$repo/output/ats_boards_cache.json" "`$repo/output/job_backlog.json" \
  "`$repo/output/vps_generation_jobs.json" \
  "`$repo/scripts/vps_search_sync.sh" "`$repo/scripts/vps_continuous_search_sync.sh"; do
  if [ -e "`$path" ]; then
    stat -c '%F|%n|%s bytes|%y' "`$path"
  fi
done
printf '%s\n' '=== SCHEDULING AND PROCESSES ==='
crontab -l 2>/dev/null | grep -E 'job-app-automation-daily-search|vps_search_sync|job-app-search-sync' || true
pgrep -af '[v]ps_continuous_search_sync|[v]ps_search_sync[.]sh|[s]earch_applications|[s]earch_documents' |
  awk -v self="`$`$" '`$1 != self' || true
if [ "`$mode" = audit ]; then
  printf '%s\n' 'JOB_SEARCH_SERVICE_AUDIT_COMPLETE'
  exit 0
fi
systemctl disable "`$unit" 2>/dev/null || true
systemctl stop --no-block "`$unit" 2>/dev/null || true
systemctl kill --kill-who=all --signal=SIGKILL "`$unit" 2>/dev/null || true
rm -f -- "`$unit_path"
current=`$(crontab -l 2>/dev/null || true)
filtered=`$(printf '%s\n' "`$current" | grep -Ev 'job-app-automation-daily-search|vps_search_sync|job-app-search-sync' || true)
if [ -n "`$filtered" ]; then
  printf '%s\n' "`$filtered" | crontab -
else
  crontab -r 2>/dev/null || true
fi
rm -f -- "`$deploy_key" "`$deploy_key.pub" "`$lock_file"
if [ -d "`$sync_tree" ]; then
  git -C "`$repo" worktree remove --force "`$sync_tree" 2>/dev/null || rm -rf -- "`$sync_tree"
fi
rm -f -- \
  "`$repo/output/vps_sync.log" \
  "`$repo/output/vps_run_status.json" \
  "`$repo/output/job_search_coverage.json" \
  "`$repo/output/ai_jobs.csv" \
  "`$repo/output/ats_boards_cache.json" \
  "`$repo/output/job_backlog.json" \
  "`$repo/output/vps_generation_jobs.json" \
  "`$repo/scripts/vps_search_sync.sh" \
  "`$repo/scripts/vps_continuous_search_sync.sh"
find /tmp -maxdepth 1 -type f -name 'job-app-search-sync-*.service' -delete
systemctl daemon-reload
systemctl reset-failed "`$unit" 2>/dev/null || true
printf '%s\n' '=== VERIFICATION ==='
systemctl show "`$unit" --property=LoadState,UnitFileState,ActiveState,SubState,MainPID 2>/dev/null || true
if systemctl cat "`$unit" --no-pager >/dev/null 2>&1; then
  printf '%s\n' 'SEARCH_UNIT_STILL_PRESENT'
  exit 1
fi
for path in "`$unit_path" "`$deploy_key" "`$deploy_key.pub" "`$sync_tree" "`$lock_file" \
  "`$repo/output/vps_sync.log" "`$repo/output/vps_run_status.json" \
  "`$repo/output/job_search_coverage.json" "`$repo/output/ai_jobs.csv" \
  "`$repo/output/ats_boards_cache.json" "`$repo/output/job_backlog.json" \
  "`$repo/output/vps_generation_jobs.json" \
  "`$repo/scripts/vps_search_sync.sh" "`$repo/scripts/vps_continuous_search_sync.sh"; do
  if [ -e "`$path" ]; then
    printf 'RELATED_PATH_STILL_PRESENT=%s\n' "`$path"
    exit 1
  fi
done
if crontab -l 2>/dev/null | grep -Eq 'job-app-automation-daily-search|vps_search_sync|job-app-search-sync'; then
  printf '%s\n' 'SEARCH_SCHEDULE_STILL_PRESENT'
  exit 1
fi
if pgrep -af '[v]ps_continuous_search_sync|[v]ps_search_sync[.]sh|[s]earch_applications|[s]earch_documents' |
  awk -v self="`$`$" '`$1 != self { found=1 } END { exit !found }'; then
  printf '%s\n' 'SEARCH_PROCESS_STILL_PRESENT'
  exit 1
fi
printf '%s\n' 'JOB_SEARCH_SERVICE_AND_RUNTIME_FILES_REMOVED'
"@

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
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
