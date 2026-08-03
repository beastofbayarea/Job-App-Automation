param(
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-worker-restart"
$Units = @(
    "job-app-greenhouse-failed-core-product-management.service",
    "job-app-greenhouse-failed-growth-general-marketing.service",
    "job-app-greenhouse-failed-product-marketing-gtm.service",
    "job-app-greenhouse-failed-program-project-management.service",
    "job-app-greenhouse-failed-technical-ai-platform-product-management.service"
)
$UnitNames = $Units -join " "
$RemoteCommand = @"
set -eu
for unit in $UnitNames; do
  path="/etc/systemd/system/`$unit"
  test -f "`$path"
  sed -i 's/^Restart=.*/Restart=always/; s/ --pause-on-unconfirmed//g' "`$path"
done
systemctl daemon-reload
systemctl enable $UnitNames
systemctl reset-failed $UnitNames || true
systemctl restart $UnitNames
sleep 10
for unit in $UnitNames; do
  systemctl show "`$unit" --property=Id,ActiveState,SubState,NRestarts,Restart,ExecMainStartTimestamp
  systemctl cat "`$unit" | grep -F -- '--skip-cover-letter'
  if systemctl cat "`$unit" | grep -F -- '--pause-on-unconfirmed'; then exit 1; fi
done
"@

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { throw "Failed to enforce always-restart worker policy" }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
