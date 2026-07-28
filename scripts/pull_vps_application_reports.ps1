# Downloads private VPS submission and failure reports without using Git.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$Destination = "output/vps_reports",
    [switch]$Overwrite
)

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}
if (-not $RemoteRepoPath.StartsWith("/") -or $RemoteRepoPath.Contains("`r") -or
    $RemoteRepoPath.Contains("`n") -or $RemoteRepoPath.Contains([char]0)) {
    Write-Error "RemoteRepoPath must be an absolute POSIX path without control characters."
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
    Write-Error "$ConfigPath contains an invalid vps.ssh_port"
    exit 1
}

$PscpCmd = Get-Command pscp -ErrorAction SilentlyContinue
if (-not $PscpCmd) {
    Write-Error "pscp.exe must be available on PATH."
    exit 1
}

$ReportNames = @(
    "submission_log.json",
    "vps_application_failures.json"
)
$DestinationPath = [IO.Path]::GetFullPath($Destination)
foreach ($Name in $ReportNames) {
    $Existing = Join-Path $DestinationPath $Name
    if ((Test-Path -LiteralPath $Existing) -and -not $Overwrite) {
        Write-Error "Local report already exists: $Existing. Use -Overwrite to replace both reports."
        exit 1
    }
}

$TemporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) "vps-reports-$([guid]::NewGuid().ToString('N'))"
$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) "vps-report-auth-$([guid]::NewGuid().ToString('N')).txt"
$RemoteRoot = $RemoteRepoPath.TrimEnd("/")
$RemoteExitCode = 0
try {
    New-Item -ItemType Directory -Path $TemporaryDirectory -ErrorAction Stop | Out-Null
    [IO.File]::WriteAllText(
        $PasswordFile,
        $SshPassword,
        [Text.UTF8Encoding]::new($false)
    )
    foreach ($Name in $ReportNames) {
        $RemoteSource = "$SshUser@${VpsHost}:$RemoteRoot/output/$Name"
        $TemporaryTarget = Join-Path $TemporaryDirectory $Name
        & $PscpCmd.Source -batch -P $SshPort -hostkey $SshHostKey -pwfile $PasswordFile `
            $RemoteSource $TemporaryTarget
        if ($LASTEXITCODE -ne 0) {
            $RemoteExitCode = $LASTEXITCODE
            break
        }
        try {
            $Payload = Get-Content -LiteralPath $TemporaryTarget -Raw | ConvertFrom-Json
            if ($null -eq $Payload) {
                throw "empty JSON"
            }
        } catch {
            Write-Error "Downloaded VPS report is not valid JSON: $Name"
            $RemoteExitCode = 1
            break
        }
    }

    if ($RemoteExitCode -eq 0) {
        New-Item -ItemType Directory -Path $DestinationPath -Force | Out-Null
        foreach ($Name in $ReportNames) {
            $TemporaryTarget = Join-Path $TemporaryDirectory $Name
            $FinalTarget = Join-Path $DestinationPath $Name
            Move-Item -LiteralPath $TemporaryTarget -Destination $FinalTarget -Force
        }
    }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $TemporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
}

if ($RemoteExitCode -ne 0) {
    Write-Error "Private VPS report download failed (exit code $RemoteExitCode); local reports were unchanged."
    exit $RemoteExitCode
}

Write-Host "Downloaded private VPS reports to $DestinationPath`: $($ReportNames -join ', ')"
