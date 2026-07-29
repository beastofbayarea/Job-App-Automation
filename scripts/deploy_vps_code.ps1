# scripts/deploy_vps_code.ps1
# Pulls the latest main branch commit onto the VPS remote repository.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json"
)

. "$PSScriptRoot\vps_script_helpers.ps1"

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}

$Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$VpsHost = [string]$Config.vps.host
$SshUser = [string]$Config.vps.ssh_user
$SshPassword = [string]$Config.vps.ssh_password.value
$SshHostKey = [string]$Config.vps.ssh_host_key
$SshPort = if ($null -ne $Config.vps.ssh_port) { [int]$Config.vps.ssh_port } else { 22 }

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
if (-not $PlinkCmd) {
    Write-Error "plink.exe not found on PATH."
    exit 1
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$RemoteCommand = "git -C $Repo pull origin main"

Write-Host "Deploying latest commit to VPS ($VpsHost)..."

$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "vps-deploy-$([guid]::NewGuid().ToString('N')).txt"
try {
    [IO.File]::WriteAllText(
        $PasswordFile,
        $SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    & $PlinkCmd.Source -ssh -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile "$SshUser@$VpsHost" $RemoteCommand
    $ExitCode = $LASTEXITCODE
} finally {
    if (Test-Path -LiteralPath $PasswordFile) {
        Remove-Item -LiteralPath $PasswordFile -Force
    }
}

if ($ExitCode -eq 0) {
    Write-Host "Deployment successful!"
} else {
    Write-Error "VPS deployment failed with exit code $ExitCode"
}
exit $ExitCode
