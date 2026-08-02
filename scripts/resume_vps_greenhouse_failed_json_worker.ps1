param(
    [ValidateSet(
        "core-product-management",
        "growth-general-marketing",
        "product-marketing-gtm",
        "program-project-management",
        "technical-ai-platform-product-management"
    )]
    [string]$Worker,
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-resume"
$Unit = "job-app-greenhouse-failed-$Worker.service"
$RemoteCommand = "systemctl reset-failed '$Unit'; systemctl start '$Unit'; systemctl show '$Unit' --property=Id,ActiveState,SubState,NRestarts,MainPID"

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)", $RemoteCommand
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
