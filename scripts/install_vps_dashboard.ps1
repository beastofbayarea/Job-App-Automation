# Installs the authenticated loopback dashboard and restores Nginx routing.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$ServiceTemplatePath = "scripts/job-app-dashboard.service.template",
    [string]$DashboardEnvironmentPath = "config/dashboard.env"
)

. "$PSScriptRoot\vps_script_helpers.ps1"

foreach ($RequiredPath in @($ConfigPath, $ServiceTemplatePath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        Write-Error "Required installation input not found: $RequiredPath"
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

if (-not (Test-Path -LiteralPath $DashboardEnvironmentPath)) {
    $RandomBytes = [byte[]]::new(24)
    [Security.Cryptography.RandomNumberGenerator]::Fill($RandomBytes)
    $DashboardPassword = [Convert]::ToBase64String($RandomBytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    $Environment = (
        "JOB_APP_DASHBOARD_USERNAME=skybison`n" +
        "JOB_APP_DASHBOARD_PASSWORD=$DashboardPassword`n"
    )
    $EnvironmentDirectory = Split-Path -Parent $DashboardEnvironmentPath
    if ($EnvironmentDirectory) {
        [IO.Directory]::CreateDirectory($EnvironmentDirectory) | Out-Null
    }
    [IO.File]::WriteAllText(
        $DashboardEnvironmentPath,
        $Environment,
        [Text.UTF8Encoding]::new($false)
    )
}

$Token = [guid]::NewGuid().ToString("N")
$RemoteUnitStage = "/tmp/vps-dashboard-$Token.service"
$RemoteEnvironmentStage = "/tmp/vps-dashboard-$Token.env"
$RenderedUnitPath = Join-Path ([IO.Path]::GetTempPath()) "vps-dashboard-$Token.service"
$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "vps-dashboard-$Token.txt"
$RenderedUnit = (
    Get-Content -LiteralPath $ServiceTemplatePath -Raw
).Replace("__REPO_DIR__", $RemoteRepoPath)
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$UnitStage = ConvertTo-PosixShellLiteral $RemoteUnitStage
$EnvironmentStage = ConvertTo-PosixShellLiteral $RemoteEnvironmentStage

$RemoteCommand = @"
set -eu
repo=$Repo
git -C "`$repo" pull --ff-only origin main
test -x "`$repo/.venv/bin/python"
test -f "`$repo/src/job_application_automation/dashboard/server.py"
install -d -m 0700 "`$repo/config"
install -m 0600 $EnvironmentStage "`$repo/config/dashboard.env"
install -m 0644 $UnitStage /etc/systemd/system/vps-dashboard.service
rm -f $EnvironmentStage $UnitStage
systemctl daemon-reload
systemctl enable vps-dashboard.service
systemctl restart vps-dashboard.service
set -a
. "`$repo/config/dashboard.env"
set +a
for attempt in `$(seq 1 20); do
  if systemctl is-active --quiet vps-dashboard.service &&
     curl --fail --silent --show-error --output /dev/null \
       --user "`$JOB_APP_DASHBOARD_USERNAME:`$JOB_APP_DASHBOARD_PASSWORD" \
       http://127.0.0.1:8000/; then
    break
  fi
  sleep 1
done
systemctl is-active vps-dashboard.service
ss -lntp | grep -F '127.0.0.1:8000'
if command -v nginx >/dev/null 2>&1; then
  nginx -t
  systemctl reset-failed nginx.service || true
  systemctl restart nginx.service
  systemctl is-active nginx.service
fi
"@

try {
    [IO.File]::WriteAllText(
        $RenderedUnitPath,
        $RenderedUnit,
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        $PasswordFile,
        $SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    foreach ($Transfer in @(
        @($RenderedUnitPath, "$SshUser@${VpsHost}:$RemoteUnitStage"),
        @($DashboardEnvironmentPath, "$SshUser@${VpsHost}:$RemoteEnvironmentStage")
    )) {
        $Result = Invoke-ExternalCommandWithTimeout `
            -FilePath $PscpCmd.Source `
            -ArgumentList @(
                "-batch",
                "-P",
                $SshPort,
                "-hostkey",
                $SshHostKey,
                "-pwfile",
                $PasswordFile,
                $Transfer[0],
                $Transfer[1]
            ) `
            -TimeoutSeconds 30
        if ($Result.ExitCode -ne 0) {
            Write-Error "Dashboard installation upload failed (exit code $($Result.ExitCode))."
            exit $Result.ExitCode
        }
    }
    $Result = Invoke-ExternalCommandWithTimeout `
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
        -TimeoutSeconds 90
    foreach ($OutputLine in $Result.Output) {
        Write-Output ([string]$OutputLine)
    }
    if ($Result.ExitCode -ne 0) {
        Write-Error "Dashboard installation failed (exit code $($Result.ExitCode))."
        exit $Result.ExitCode
    }
} finally {
    Remove-Item -LiteralPath $RenderedUnitPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

Write-Host (
    "Installed the authenticated dashboard on 127.0.0.1:8000 and restored " +
    "validated Nginx routing. Credentials remain in ignored $DashboardEnvironmentPath."
)
