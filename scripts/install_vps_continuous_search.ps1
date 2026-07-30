# Installs or repairs the continuous job-discovery and safe-publication service.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$ServiceTemplatePath = "scripts/job-app-search-sync.service.template"
)

. "$PSScriptRoot\vps_script_helpers.ps1"

foreach ($RequiredPath in @(
    $ConfigPath,
    $ServiceTemplatePath
)) {
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

$ServiceName = "job-app-search-sync.service"
$Token = [guid]::NewGuid().ToString("N")
$RemoteUnitStage = "/tmp/job-app-search-sync-$Token.service"
$RenderedUnitPath = Join-Path ([IO.Path]::GetTempPath()) "job-app-search-sync-$Token.service"
$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "job-app-search-sync-$Token.txt"
$RenderedUnit = (
    Get-Content -LiteralPath $ServiceTemplatePath -Raw
).Replace("__REPO_DIR__", $RemoteRepoPath)
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$UnitStage = ConvertTo-PosixShellLiteral $RemoteUnitStage
$CronMarker = "job-app-automation-daily-search"

$RemoteCommand = @"
set -eu
repo=$Repo
git -C "`$repo" pull --ff-only origin main
test -f "`$repo/scripts/vps_search_sync.sh"
test -f "`$repo/scripts/vps_continuous_search_sync.sh"
install -d -m 0700 "`$repo/output"
install -m 0644 $UnitStage "/etc/systemd/system/$ServiceName"
rm -f $UnitStage
current=`$(crontab -l 2>/dev/null || true)
filtered=`$(printf '%s\n' "`$current" | grep -v '# $CronMarker' || true)
if [ -n "`$filtered" ]; then
  printf '%s\n' "`$filtered" | crontab -
else
  crontab -r 2>/dev/null || true
fi
systemctl daemon-reload
systemctl enable "$ServiceName"
systemctl restart "$ServiceName"
systemctl is-active "$ServiceName"
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
    Write-Host "Staging continuous search-sync unit to $VpsHost..."
    $Transfer = Invoke-ExternalCommandWithTimeout `
        -FilePath $PscpCmd.Source `
        -ArgumentList @(
            "-batch",
            "-P",
            $SshPort,
            "-pwfile",
            $PasswordFile,
            "-hostkey",
            $SshHostKey,
            $RenderedUnitPath,
            "$SshUser@${VpsHost}:$RemoteUnitStage"
        ) `
        -TimeoutSeconds 30
    if ($Transfer.ExitCode -ne 0) {
        Write-Error "Continuous search unit upload failed (exit code $($Transfer.ExitCode))."
        exit $Transfer.ExitCode
    }

    Write-Host "Installing, disabling cron, and starting $ServiceName on $VpsHost..."
    $Result = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkCmd.Source `
        -ArgumentList @(
            "-ssh",
            "-batch",
            "-P",
            $SshPort,
            "-pwfile",
            $PasswordFile,
            "-hostkey",
            $SshHostKey,
            "$SshUser@$VpsHost",
            $RemoteCommand
        ) `
        -TimeoutSeconds 60

    if ($Result.ExitCode -ne 0) {
        Write-Error "Continuous search-sync installation failed on $VpsHost (exit code $($Result.ExitCode))."
        exit $Result.ExitCode
    }

    Write-Host "Successfully installed and started $ServiceName on $VpsHost!"
    foreach ($OutputLine in $Result.Output) {
        Write-Output ([string]$OutputLine)
    }
} finally {
    Remove-Item -LiteralPath $RenderedUnitPath -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PasswordFile -ErrorAction SilentlyContinue
}
