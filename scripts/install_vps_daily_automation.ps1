# Installs the unattended daily VPS search schedule and private archive root.
param(
    [Parameter(Mandatory = $true)]
    [string]$RemoteRepoPath,
    [ValidateRange(0, 23)]
    [int]$HourUtc = 3,
    [string]$ConfigPath = "config/vps_config.json"
)

. "$PSScriptRoot\vps_script_helpers.ps1"

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}
$RemoteRepoPath = $RemoteRepoPath.TrimEnd("/")
if (-not $RemoteRepoPath.StartsWith("/")) {
    Write-Error "RemoteRepoPath must be an absolute POSIX path."
    exit 1
}

try {
    $Config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
} catch {
    Write-Error "VPS config at $ConfigPath is not valid JSON."
    exit 1
}

$VpsHost = $Config.vps.host
$SshUser = $Config.vps.ssh_user
$SshPassword = $Config.vps.ssh_password.value
$SshHostKey = $Config.vps.ssh_host_key
$SshPort = if ($null -ne $Config.vps.ssh_port) { [int]$Config.vps.ssh_port } else { 22 }
$ArchiveRoot = if ($Config.vps.document_archive_root) {
    [string]$Config.vps.document_archive_root
} else {
    "/var/lib/job-application-automation/private-archive"
}

if (-not $VpsHost -or -not $SshUser -or -not $SshPassword -or -not $SshHostKey) {
    Write-Error "$ConfigPath is missing required pinned VPS connection settings."
    exit 1
}
if ($SshPort -lt 1 -or $SshPort -gt 65535) {
    Write-Error "$ConfigPath contains an invalid vps.ssh_port"
    exit 1
}
if (-not $ArchiveRoot.StartsWith("/")) {
    Write-Error "vps.document_archive_root must be an absolute POSIX path."
    exit 1
}

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
if (-not $PlinkCmd) {
    Write-Error "plink.exe not found on PATH. Install PuTTY or add it to PATH."
    exit 1
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$Archive = ConvertTo-PosixShellLiteral $ArchiveRoot
$Marker = "job-app-automation-daily-search"
$CronCommand = "0 $HourUtc * * * $RemoteRepoPath/scripts/vps_search_sync.sh >> $RemoteRepoPath/output/vps_sync.log 2>&1 # $Marker"
$Cron = ConvertTo-PosixShellLiteral $CronCommand
$RemoteCommand = @"
set -eu
test -x $Repo/scripts/vps_search_sync.sh
install -d -m 0700 $Archive
current=`$(crontab -l 2>/dev/null || true)
filtered=`$(printf '%s\n' "`$current" | grep -v '# $Marker' || true)
{ printf '%s\n' "`$filtered"; printf '%s\n' $Cron; } | sed '/^$/d' | crontab -
bash $Repo/scripts/install_vps_logrotate.sh
crontab -l | grep '# $Marker'
"@

$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "job-app-install-$([guid]::NewGuid().ToString('N')).txt"
try {
    [IO.File]::WriteAllText(
        $PasswordFile,
        [string]$SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    & $PlinkCmd.Source -ssh -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile `
        "$SshUser@$VpsHost" $RemoteCommand
    $RemoteExitCode = $LASTEXITCODE
} finally {
    if (Test-Path -LiteralPath $PasswordFile) {
        Remove-Item -LiteralPath $PasswordFile -Force
    }
}

if ($RemoteExitCode -ne 0) {
    Write-Error "VPS automation installation failed (exit code $RemoteExitCode)."
    exit $RemoteExitCode
}

Write-Host "Installed daily VPS search at $($HourUtc.ToString('00')):00 UTC and secured $ArchiveRoot."
