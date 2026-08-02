# Installs three coordinated Excel-backed Greenhouse workers and retires the
# superseded single-Excel, SmartRecruiters, and Workable application workers.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$ServiceTemplatePath = "scripts/templates/job-app-greenhouse-source.service.template",
    [ValidateRange(30, 1800)]
    [int]$TimeoutSeconds = 900
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

$Workers = @(
    [pscustomobject]@{ Id = "all"; Workbook = "data/greenhouse_all_jobs.xlsx"; RemoteWorkbook = "greenhouse_all_jobs.xlsx" },
    [pscustomobject]@{ Id = "marketing"; Workbook = "data/greenhouse_marketing_jobs.xlsx"; RemoteWorkbook = "greenhouse_marketing_jobs.xlsx" },
    [pscustomobject]@{ Id = "product-management"; Workbook = "data/greenhouse_product_management_jobs.xlsx"; RemoteWorkbook = "greenhouse_product_management_jobs.xlsx" }
)

foreach ($RequiredPath in @($ConfigPath, $ServiceTemplatePath) + $Workers.Workbook) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        Write-Error "Required deployment input not found: $RequiredPath"
        exit 1
    }
}
$RemoteRepoPath = $RemoteRepoPath.TrimEnd("/")
if (-not $RemoteRepoPath.StartsWith("/") -or $RemoteRepoPath -match "\s") {
    Write-Error "RemoteRepoPath must be an absolute POSIX path without whitespace."
    exit 1
}

try { $Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json } catch {
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
$PscpCmd = Get-Command pscp -ErrorAction SilentlyContinue
if (-not $PlinkCmd -or -not $PscpCmd) {
    Write-Error "plink.exe and pscp.exe must be available on PATH."
    exit 1
}

$Token = [guid]::NewGuid().ToString("N")
$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "greenhouse-excel-fleet-$Token.txt"
$Template = Get-Content -LiteralPath $ServiceTemplatePath -Raw
$Transfers = [Collections.Generic.List[object]]::new()
$TemporaryFiles = [Collections.Generic.List[string]]::new()
$ServiceNames = [Collections.Generic.List[string]]::new()

foreach ($Worker in $Workers) {
    $ServiceName = "job-app-greenhouse-excel-$($Worker.Id).service"
    $ServiceNames.Add($ServiceName)
    $RemoteWorkbookPath = "$RemoteRepoPath/data/$($Worker.RemoteWorkbook)"
    $PeerArguments = @("--peer-state $RemoteRepoPath/output/continuous_greenhouse_state.json")
    foreach ($Peer in $Workers | Where-Object Id -ne $Worker.Id) {
        $PeerArguments += "--peer-state $RemoteRepoPath/output/continuous_greenhouse_excel_$($Peer.Id)_state.json"
    }
    $SourceArgs = (
        "--ats-platform greenhouse --source tracker --worker-id excel-$($Worker.Id) " +
        "--sleep-min-seconds 5 --sleep-max-seconds 15 --tracker $RemoteWorkbookPath " +
        "--state $RemoteRepoPath/output/continuous_greenhouse_excel_$($Worker.Id)_state.json " +
        ($PeerArguments -join " ") + " " +
        "--claims $RemoteRepoPath/output/continuous_greenhouse_claims.json " +
        "--selected-input $RemoteRepoPath/output/continuous_greenhouse_excel_$($Worker.Id)_selected.json " +
        "--results-dir $RemoteRepoPath/output/continuous_greenhouse_excel_$($Worker.Id)_results " +
        "--documents-dir $RemoteRepoPath/output/continuous_greenhouse_excel_$($Worker.Id)_documents"
    )
    $Unit = $Template.Replace("__DESCRIPTION__", "Continuous guarded Greenhouse $($Worker.Id) Excel application worker").Replace(
        "__REPO_DIR__", $RemoteRepoPath
    ).Replace("__SOURCE_PRECHECK__", "ExecStartPre=/usr/bin/test -r $RemoteWorkbookPath").Replace(
        "__SOURCE_ARGS__", $SourceArgs
    ).Replace("__SYSLOG_IDENTIFIER__", "job-app-greenhouse-excel-$($Worker.Id)")
    $LocalUnit = Join-Path ([IO.Path]::GetTempPath()) "$ServiceName-$Token"
    [IO.File]::WriteAllText($LocalUnit, $Unit, [Text.UTF8Encoding]::new($false))
    $TemporaryFiles.Add($LocalUnit)
    $Transfers.Add([pscustomobject]@{ Local = $Worker.Workbook; Remote = "/tmp/$($Worker.RemoteWorkbook)-$Token" })
    $Transfers.Add([pscustomobject]@{ Local = $LocalUnit; Remote = "/tmp/$ServiceName-$Token" })
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$InstallLines = [Collections.Generic.List[string]]::new()
$ValidateLines = [Collections.Generic.List[string]]::new()
foreach ($Worker in $Workers) {
    $ServiceName = "job-app-greenhouse-excel-$($Worker.Id).service"
    $InstallLines.Add("install -m 0600 '/tmp/$($Worker.RemoteWorkbook)-$Token' ""`$repo/data/$($Worker.RemoteWorkbook)""")
    $InstallLines.Add("install -m 0644 '/tmp/$ServiceName-$Token' '/etc/systemd/system/$ServiceName'")
    $ValidateLines.Add(
        "PYTHONPATH=""`$repo/src"" ""`$repo/.venv/bin/python"" -m job_application_automation.core.continuous_source_ats " +
        "--ats-platform greenhouse --source tracker --worker-id validate-$($Worker.Id) " +
        "--tracker ""`$repo/data/$($Worker.RemoteWorkbook)"" --state ""`$repo/output/validate_$($Worker.Id)_state.json"" " +
        "--selected-input ""`$repo/output/validate_$($Worker.Id)_selected.json"" --results-dir ""`$repo/output/validate_$($Worker.Id)_results"" " +
        "--documents-dir ""`$repo/output/validate_$($Worker.Id)_documents"" --validate-only"
    )
}
$InstallBlock = $InstallLines -join "`n"
$ValidateBlock = $ValidateLines -join "`n"
$NewServices = $ServiceNames -join " "
$RemoteCommand = @"
set -eu
repo=$Repo
git -C "`$repo" pull --ff-only origin main
test -x "`$repo/.venv/bin/python"
test -r "`$repo/config/candidate_profile_config.json"
test -r "`$repo/config/candidate_email_pool.json"
test -r "`$repo/config/vertex_service_account.json"
install -d -m 0700 "`$repo/data" "`$repo/output"
$InstallBlock
rm -f /tmp/greenhouse_*_jobs.xlsx-$Token /tmp/job-app-greenhouse-excel-*.service-$Token
$ValidateBlock
systemd-analyze verify $NewServices
for attempt in `$(seq 1 120); do
  if ! pgrep -f '[j]ob_automation.py apply' >/dev/null; then break; fi
  sleep 5
done
if pgrep -f '[j]ob_automation.py apply' >/dev/null; then
  printf '%s\n' 'An ATS application is still active; refusing to interrupt it.' >&2
  exit 76
fi
systemctl disable --now job-app-greenhouse-excel.service job-app-smartrecruiters.service job-app-workable.service
systemctl daemon-reload
systemctl enable $NewServices
systemctl restart $NewServices
for unit in $NewServices; do systemctl is-active --quiet "`$unit"; done
for unit in job-app-greenhouse-excel.service job-app-smartrecruiters.service job-app-workable.service; do
  test "`$(systemctl is-active "`$unit" || true)" = inactive
  test "`$(systemctl is-enabled "`$unit" || true)" = disabled
done
systemctl show job-app-greenhouse.service $NewServices job-app-greenhouse-excel.service job-app-smartrecruiters.service job-app-workable.service \
  --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts
pgrep -af '[c]ontinuous_source_ats.*--ats-platform greenhouse' || true
"@
$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand

try {
    [IO.File]::WriteAllText($PasswordFile, $SshPassword, [Text.UTF8Encoding]::new($false))
    foreach ($Transfer in $Transfers) {
        & $PscpCmd.Source -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile `
            $Transfer.Local "$SshUser@${VpsHost}:$($Transfer.Remote)"
        if ($LASTEXITCODE -ne 0) { throw "VPS upload failed with exit code $LASTEXITCODE" }
    }
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkCmd.Source -ArgumentList @(
        "-ssh", "-batch", "-P", $SshPort, "-hostkey", $SshHostKey, "-pwfile", $PasswordFile,
        "$SshUser@$VpsHost", $RemoteCommand
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) {
        Write-Error "Greenhouse Excel fleet installation failed (exit code $($Execution.ExitCode))."
        exit $Execution.ExitCode
    }
} finally {
    foreach ($TemporaryFile in $TemporaryFiles) { Remove-Item -LiteralPath $TemporaryFile -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

Write-Host "Installed three coordinated Greenhouse Excel workers and disabled the superseded application services."
