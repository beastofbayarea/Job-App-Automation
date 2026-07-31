# Replaces Ashby with a second, Excel-backed Greenhouse worker while preserving
# the existing search-backed Greenhouse service.
param(
    [string]$WorkbookPath = "data/greenhouse_roles.xlsx",
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$ServiceTemplatePath = "scripts/job-app-greenhouse-source.service.template",
    [ValidateRange(30, 1800)]
    [int]$TimeoutSeconds = 900
)

. "$PSScriptRoot\vps_script_helpers.ps1"

foreach ($RequiredPath in @($WorkbookPath, $ConfigPath, $ServiceTemplatePath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        Write-Error "Required deployment input not found: $RequiredPath"
        exit 1
    }
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
$PscpCmd = Get-Command pscp -ErrorAction SilentlyContinue
if (-not $PlinkCmd -or -not $PscpCmd) {
    Write-Error "plink.exe and pscp.exe must be available on PATH."
    exit 1
}

$Token = [guid]::NewGuid().ToString("N")
$RemoteWorkbookStage = "/tmp/greenhouse-roles-$Token.xlsx"
$RemoteSearchUnitStage = "/tmp/job-app-greenhouse-$Token.service"
$RemoteExcelUnitStage = "/tmp/job-app-greenhouse-excel-$Token.service"
$SearchUnitPath = Join-Path ([IO.Path]::GetTempPath()) "job-app-greenhouse-$Token.service"
$ExcelUnitPath = Join-Path ([IO.Path]::GetTempPath()) "job-app-greenhouse-excel-$Token.service"
$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "greenhouse-parallel-$Token.txt"
$Template = Get-Content -LiteralPath $ServiceTemplatePath -Raw

$SearchArgs = (
    "--ats-platform greenhouse --source search --worker-id search " +
    "--sleep-min-seconds 5 --sleep-max-seconds 15 " +
    "--state $RemoteRepoPath/output/continuous_greenhouse_state.json " +
    "--peer-state $RemoteRepoPath/output/continuous_greenhouse_excel_state.json " +
    "--claims $RemoteRepoPath/output/continuous_greenhouse_claims.json " +
    "--selected-input $RemoteRepoPath/output/continuous_greenhouse_search_selected.json " +
    "--results-dir $RemoteRepoPath/output/continuous_greenhouse_results " +
    "--documents-dir $RemoteRepoPath/output/continuous_greenhouse_documents"
)
$ExcelArgs = (
    "--ats-platform greenhouse --source tracker --worker-id excel " +
    "--sleep-min-seconds 5 --sleep-max-seconds 15 " +
    "--tracker $RemoteRepoPath/data/greenhouse_roles.xlsx " +
    "--state $RemoteRepoPath/output/continuous_greenhouse_excel_state.json " +
    "--peer-state $RemoteRepoPath/output/continuous_greenhouse_state.json " +
    "--claims $RemoteRepoPath/output/continuous_greenhouse_claims.json " +
    "--selected-input $RemoteRepoPath/output/continuous_greenhouse_excel_selected.json " +
    "--results-dir $RemoteRepoPath/output/continuous_greenhouse_excel_results " +
    "--documents-dir $RemoteRepoPath/output/continuous_greenhouse_excel_documents"
)
$SearchUnit = $Template.Replace(
    "__DESCRIPTION__",
    "Continuous guarded Greenhouse search-backed application worker"
).Replace(
    "__REPO_DIR__",
    $RemoteRepoPath
).Replace(
    "__SOURCE_PRECHECK__",
    "ExecStartPre=/usr/bin/test -r $RemoteRepoPath/output/vps_generation_jobs.json"
).Replace(
    "__SOURCE_ARGS__",
    $SearchArgs
).Replace(
    "__SYSLOG_IDENTIFIER__",
    "job-app-greenhouse"
)
$ExcelUnit = $Template.Replace(
    "__DESCRIPTION__",
    "Continuous guarded Greenhouse Excel-backed application worker"
).Replace(
    "__REPO_DIR__",
    $RemoteRepoPath
).Replace(
    "__SOURCE_PRECHECK__",
    "ExecStartPre=/usr/bin/test -r $RemoteRepoPath/data/greenhouse_roles.xlsx"
).Replace(
    "__SOURCE_ARGS__",
    $ExcelArgs
).Replace(
    "__SYSLOG_IDENTIFIER__",
    "job-app-greenhouse-excel"
)

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$WorkbookStage = ConvertTo-PosixShellLiteral $RemoteWorkbookStage
$SearchUnitStage = ConvertTo-PosixShellLiteral $RemoteSearchUnitStage
$ExcelUnitStage = ConvertTo-PosixShellLiteral $RemoteExcelUnitStage
$RemoteCommand = @"
set -eu
repo=$Repo
git -C "`$repo" pull --ff-only origin main
test -f "`$repo/src/job_application_automation/core/continuous_source_ats.py"
test -x "`$repo/.venv/bin/python"
test -r "`$repo/config/candidate_profile_config.json"
test -r "`$repo/config/candidate_email_pool.json"
test -r "`$repo/config/vertex_service_account.json"
install -d -m 0700 "`$repo/data" "`$repo/output"
install -m 0600 $WorkbookStage "`$repo/data/greenhouse_roles.xlsx"
install -m 0644 $SearchUnitStage /etc/systemd/system/job-app-greenhouse.service
install -m 0644 $ExcelUnitStage /etc/systemd/system/job-app-greenhouse-excel.service
rm -f $WorkbookStage $SearchUnitStage $ExcelUnitStage
PYTHONPATH="`$repo/src" "`$repo/.venv/bin/python" \
  -m job_application_automation.core.continuous_source_ats \
  --ats-platform greenhouse \
  --source tracker \
  --worker-id excel \
  --tracker "`$repo/data/greenhouse_roles.xlsx" \
  --state "`$repo/output/continuous_greenhouse_excel_state.json" \
  --peer-state "`$repo/output/continuous_greenhouse_state.json" \
  --selected-input "`$repo/output/continuous_greenhouse_excel_selected.json" \
  --results-dir "`$repo/output/continuous_greenhouse_excel_results" \
  --documents-dir "`$repo/output/continuous_greenhouse_excel_documents" \
  --validate-only
systemd-analyze verify \
  /etc/systemd/system/job-app-greenhouse.service \
  /etc/systemd/system/job-app-greenhouse-excel.service
for attempt in `$(seq 1 120); do
  if ! pgrep -f '[j]ob_automation.py apply' >/dev/null; then
    break
  fi
  sleep 5
done
if pgrep -f '[j]ob_automation.py apply' >/dev/null; then
  printf '%s\n' 'An ATS application is still active; refusing to interrupt it.' >&2
  exit 76
fi
systemctl daemon-reload
systemctl enable job-app-greenhouse.service
systemctl restart job-app-greenhouse.service
systemctl is-active --quiet job-app-greenhouse.service
systemctl enable job-app-greenhouse-excel.service
systemctl restart job-app-greenhouse-excel.service
systemctl is-active --quiet job-app-greenhouse-excel.service
systemctl disable --now job-app-ashby.service
test "`$(systemctl is-active job-app-ashby.service || true)" = inactive
test "`$(systemctl is-enabled job-app-ashby.service || true)" = disabled
printf '%s\n' '=== FINAL SERVICE STATE ==='
systemctl show \
  job-app-ashby.service \
  job-app-greenhouse.service \
  job-app-greenhouse-excel.service \
  --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp
printf '%s\n' '=== GREENHOUSE PROCESSES ==='
pgrep -af '[c]ontinuous_source_ats.*--ats-platform greenhouse' || true
"@
$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand

try {
    [IO.File]::WriteAllText(
        $SearchUnitPath,
        $SearchUnit,
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $ExcelUnitPath,
        $ExcelUnit,
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $PasswordFile,
        $SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    foreach ($Transfer in @(
        @{ Local = $WorkbookPath; Remote = "$SshUser@${VpsHost}:$RemoteWorkbookStage" },
        @{ Local = $SearchUnitPath; Remote = "$SshUser@${VpsHost}:$RemoteSearchUnitStage" },
        @{ Local = $ExcelUnitPath; Remote = "$SshUser@${VpsHost}:$RemoteExcelUnitStage" }
    )) {
        & $PscpCmd.Source -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile `
            $Transfer.Local $Transfer.Remote
        if ($LASTEXITCODE -ne 0) {
            Write-Error "VPS upload failed (exit code $LASTEXITCODE)."
            exit $LASTEXITCODE
        }
    }
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
    $RemoteExitCode = $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $SearchUnitPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ExcelUnitPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

if ($RemoteExitCode -ne 0) {
    Write-Error "Parallel Greenhouse installation failed (exit code $RemoteExitCode)."
    exit $RemoteExitCode
}

Write-Host (
    "Stopped and disabled job-app-ashby.service; installed active search-backed and " +
    "Excel-backed Greenhouse workers with shared job-identity claims."
)
