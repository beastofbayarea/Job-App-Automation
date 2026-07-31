# Installs or repairs one persistent, single-application ATS worker.
param(
    [Parameter(Mandatory = $true)]
    [string]$AtsPlatform,
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$ServiceTemplatePath = "scripts/job-app-continuous-ats.service.template"
)

. "$PSScriptRoot\vps_script_helpers.ps1"

$AtsPlatform = $AtsPlatform.Trim().ToLowerInvariant()
if ($AtsPlatform -notmatch "^[a-z][a-z0-9]*$") {
    Write-Error "AtsPlatform must contain only lowercase letters and digits."
    exit 1
}

foreach ($RequiredPath in @(
    $ConfigPath,
    $ServiceTemplatePath,
    "config/candidate_email_pool.json",
    "config/candidate_profile_config.json"
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

$ServiceName = "job-app-$AtsPlatform.service"
$Token = [guid]::NewGuid().ToString("N")
$RemoteUnitStage = "/tmp/job-app-$AtsPlatform-$Token.service"
$RemotePoolStage = "/tmp/candidate-email-pool-$Token.json"
$RemoteProfileStage = "/tmp/candidate-profile-$Token.json"
$RenderedUnitPath = Join-Path ([IO.Path]::GetTempPath()) "job-app-$AtsPlatform-$Token.service"
$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "job-app-$AtsPlatform-$Token.txt"
$RenderedUnit = (
    Get-Content -LiteralPath $ServiceTemplatePath -Raw
).Replace("__REPO_DIR__", $RemoteRepoPath).Replace("__ATS_PLATFORM__", $AtsPlatform)
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath
$UnitStage = ConvertTo-PosixShellLiteral $RemoteUnitStage
$PoolStage = ConvertTo-PosixShellLiteral $RemotePoolStage
$ProfileStage = ConvertTo-PosixShellLiteral $RemoteProfileStage
$CronMarker = "job-app-automation-daily-search"
$RemoteCommand = @"
set -eu
repo=$Repo
git -C "`$repo" pull --ff-only origin main
test -f "`$repo/src/job_application_automation/core/continuous_ats.py"
test -f "`$repo/src/job_application_automation/engines/$AtsPlatform.py"
test -f "`$repo/config/candidate_profile_config.json"
test -f "`$repo/config/vertex_service_account.json"
test -f "`$repo/data/base_resume.txt"
if ! command -v xvfb-run >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xvfb
fi
install -d -m 0700 "`$repo/config" "`$repo/output"
install -m 0600 $PoolStage "`$repo/config/candidate_email_pool.json"
install -m 0600 $ProfileStage "`$repo/config/candidate_profile_config.json"
install -m 0644 $UnitStage "/etc/systemd/system/$ServiceName"
rm -f $PoolStage $ProfileStage $UnitStage
current=`$(crontab -l 2>/dev/null || true)
filtered=`$(printf '%s\n' "`$current" | grep -v '# $CronMarker' || true)
if [ -n "`$filtered" ]; then
  printf '%s\n' "`$filtered" | crontab -
else
  crontab -r 2>/dev/null || true
fi
if systemctl is-active --quiet "$ServiceName"; then
  for attempt in `$(seq 1 120); do
    if ! pgrep -f '[j]ob_automation.py apply' >/dev/null; then
      break
    fi
    sleep 5
  done
  if pgrep -f '[j]ob_automation.py apply' >/dev/null; then
    printf '%s\n' 'An application is still active; refusing to interrupt it.' >&2
    exit 76
  fi
fi
systemctl daemon-reload
systemctl enable "$ServiceName"
systemctl restart "$ServiceName"
systemctl is-enabled "$ServiceName"
systemctl is-active "$ServiceName"
systemctl --no-pager --full status "$ServiceName" | sed -n '1,18p'
"@
$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand

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
        @{
            Local = $RenderedUnitPath
            Remote = "$SshUser@${VpsHost}:$RemoteUnitStage"
        },
        @{
            Local = "config/candidate_email_pool.json"
            Remote = "$SshUser@${VpsHost}:$RemotePoolStage"
        },
        @{
            Local = "config/candidate_profile_config.json"
            Remote = "$SshUser@${VpsHost}:$RemoteProfileStage"
        }
    )) {
        & $PscpCmd.Source -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile `
            $Transfer.Local $Transfer.Remote
        if ($LASTEXITCODE -ne 0) {
            Write-Error "VPS installation upload failed (exit code $LASTEXITCODE)."
            exit $LASTEXITCODE
        }
    }
    & $PlinkCmd.Source -ssh -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile `
        "$SshUser@$VpsHost" $RemoteCommand
    $RemoteExitCode = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $RenderedUnitPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

if ($RemoteExitCode -ne 0) {
    Write-Error "Continuous $AtsPlatform installation failed (exit code $RemoteExitCode)."
    exit $RemoteExitCode
}

Write-Host (
    "Installed and started $ServiceName without stopping or restarting other ATS " +
    "workers or the continuous job-search service. The replaced daily cron remains disabled."
)
