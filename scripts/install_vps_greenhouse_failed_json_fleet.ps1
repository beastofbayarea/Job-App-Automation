param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$ServiceTemplatePath = "scripts/templates/job-app-greenhouse-source.service.template",
    [ValidateRange(60, 1800)]
    [int]$TimeoutSeconds = 900
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"

$Sources = @(
    "core_product_management",
    "growth_general_marketing",
    "product_marketing_gtm",
    "program_project_management",
    "technical_ai_platform_product_management"
)
$SourceFiles = @($Sources | ForEach-Object { "data/greenhouse_failed_$_.json" })
foreach ($Path in @($ConfigPath, $ServiceTemplatePath) + $SourceFiles) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Required deployment input not found: $Path" }
}

$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PscpPath = Get-RequiredCommandPath -Name "pscp"
$RepoPath = $RemoteRepoPath.TrimEnd("/")
$Repo = ConvertTo-PosixShellLiteral $RepoPath
$Token = [guid]::NewGuid().ToString("N")
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "greenhouse-failed-json"
$Template = (Get-Content -LiteralPath $ServiceTemplatePath -Raw).Replace("Restart=always", "Restart=on-failure")
$TemporaryFiles = [Collections.Generic.List[string]]::new()
$Transfers = [Collections.Generic.List[object]]::new()
$Units = [Collections.Generic.List[string]]::new()

try {
    foreach ($Source in $Sources) {
        $Unit = "job-app-greenhouse-failed-$($Source.Replace('_', '-')).service"
        $Units.Add($Unit)
        $RemoteSource = "$RepoPath/data/greenhouse_failed_$Source.json"
        $State = "$RepoPath/output/continuous_greenhouse_failed_${Source}_state.json"
        $PeerArgs = @($Sources | Where-Object { $_ -ne $Source } | ForEach-Object {
            "--peer-state $RepoPath/output/continuous_greenhouse_failed_${_}_state.json"
        })
        $Args = (
            "--ats-platform greenhouse --source failed-json --worker-id failed-$($Source.Replace('_', '-')) " +
            "--skip-cover-letter --input $RemoteSource " +
            "--sleep-min-seconds 5 --sleep-max-seconds 15 --state $State " +
            ($PeerArgs -join " ") + " --claims $RepoPath/output/continuous_greenhouse_failed_claims.json " +
            "--selected-input $RepoPath/output/continuous_greenhouse_failed_${Source}_selected.json " +
            "--results-dir $RepoPath/output/continuous_greenhouse_failed_${Source}_results " +
            "--documents-dir $RepoPath/output/continuous_greenhouse_failed_${Source}_documents"
        )
        $UnitText = $Template.Replace("__DESCRIPTION__", "Supervised Greenhouse failed JSON worker ($Source)").Replace(
            "__REPO_DIR__", $RepoPath
        ).Replace("__SOURCE_PRECHECK__", "ExecStartPre=/usr/bin/test -r $RemoteSource").Replace(
            "__SOURCE_ARGS__", $Args
        ).Replace("__SYSLOG_IDENTIFIER__", $Unit.Replace(".service", ""))
        $LocalUnit = Join-Path ([IO.Path]::GetTempPath()) "$Unit-$Token"
        [IO.File]::WriteAllText($LocalUnit, $UnitText, [Text.UTF8Encoding]::new($false))
        $TemporaryFiles.Add($LocalUnit)
        $Transfers.Add([pscustomobject]@{ Local = "data/greenhouse_failed_$Source.json"; Remote = "/tmp/greenhouse_failed_$Source.json-$Token" })
        $Transfers.Add([pscustomobject]@{ Local = $LocalUnit; Remote = "/tmp/$Unit-$Token" })
    }

    foreach ($Transfer in $Transfers) {
        & $PscpPath -batch -P $Connection.Port -hostkey $Connection.HostKey -pwfile $PasswordFile `
            $Transfer.Local "$($Connection.User)@$($Connection.Host):$($Transfer.Remote)"
        if ($LASTEXITCODE -ne 0) { throw "VPS upload failed with exit code $LASTEXITCODE" }
    }

    $Install = foreach ($Source in $Sources) {
        $Unit = "job-app-greenhouse-failed-$($Source.Replace('_', '-')).service"
        "install -m 0600 '/tmp/greenhouse_failed_$Source.json-$Token' '$RepoPath/data/greenhouse_failed_$Source.json'`ninstall -m 0644 '/tmp/$Unit-$Token' '/etc/systemd/system/$Unit'"
    }
    $UnitNames = $Units -join " "
    $RemoteCommand = @"
set -eu
repo=$Repo
git -C "`$repo" pull --ff-only origin main
install -d -m 0700 "`$repo/data" "`$repo/output"
$($Install -join "`n")
systemctl disable --now job-app-greenhouse.service job-app-greenhouse-excel-all.service job-app-greenhouse-excel-marketing.service job-app-greenhouse-excel-product-management.service || true
systemctl daemon-reload
systemctl enable $UnitNames
systemctl reset-failed $UnitNames || true
systemctl restart $UnitNames
sleep 10
systemctl show $UnitNames --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStatus
for unit in $UnitNames; do systemctl cat "`$unit" | grep -F -- '--skip-cover-letter'; done
"@
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { throw "Failed JSON fleet installation failed with exit code $($Execution.ExitCode)" }
} finally {
    $TemporaryFiles | ForEach-Object { Remove-Item -LiteralPath $_ -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
