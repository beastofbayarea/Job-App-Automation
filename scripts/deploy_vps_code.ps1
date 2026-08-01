# scripts/deploy_vps_code.ps1
# Pulls the latest main branch commit onto the VPS remote repository.
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
$VpsHost = $Connection.Host
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$RemoteCommand = "git -C $Repo pull --ff-only origin main"

Write-Host "Deploying latest commit to VPS ($VpsHost)..."

$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "vps-deploy"
try {
    $Execution = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkPath `
        -ArgumentList @(
            "-ssh",
            "-batch",
            "-P",
            $Connection.Port,
            "-hostkey",
            $Connection.HostKey,
            "-pwfile",
            $PasswordFile,
            "$($Connection.User)@$VpsHost",
            $RemoteCommand
        ) `
        -TimeoutSeconds $TimeoutSeconds
    foreach ($OutputLine in $Execution.Output) {
        Write-Output ([string]$OutputLine)
    }
    $ExitCode = $Execution.ExitCode
} finally {
    if (Test-Path -LiteralPath $PasswordFile) {
        Remove-Item -LiteralPath $PasswordFile -Force
    }
}

if ($ExitCode -eq 0) {
    Write-Host "Deployment successful!"
} elseif ($ExitCode -eq 124) {
    Write-Error "VPS deployment timed out after $TimeoutSeconds seconds."
} else {
    Write-Error "VPS deployment failed with exit code $ExitCode"
}
exit $ExitCode
