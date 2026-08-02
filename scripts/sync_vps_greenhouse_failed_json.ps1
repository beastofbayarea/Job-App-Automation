param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Sources = @(
    "core_product_management",
    "growth_general_marketing",
    "product_marketing_gtm",
    "program_project_management",
    "technical_ai_platform_product_management"
)
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PscpPath = Get-RequiredCommandPath -Name "pscp"
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$Token = [guid]::NewGuid().ToString("N")
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-sync"

try {
    foreach ($Source in $Sources) {
        $LocalPath = "data/greenhouse_failed_$Source.json"
        if (-not (Test-Path -LiteralPath $LocalPath -PathType Leaf)) { throw "Missing $LocalPath" }
        $Payload = Get-Content -LiteralPath $LocalPath -Raw | ConvertFrom-Json
        if ($Payload -isnot [array]) { throw "$LocalPath must contain a JSON array" }
        & $PscpPath -batch -P $Connection.Port -hostkey $Connection.HostKey -pwfile $PasswordFile `
            $LocalPath "$($Connection.User)@$($Connection.Host):/tmp/greenhouse_failed_$Source.json-$Token"
        if ($LASTEXITCODE -ne 0) { throw "Upload failed for $LocalPath" }
    }
    $Installs = $Sources | ForEach-Object {
        "python3 -m json.tool '/tmp/greenhouse_failed_$_.json-$Token' >/dev/null; install -m 0600 '/tmp/greenhouse_failed_$_.json-$Token' `$repo/data/greenhouse_failed_$_.json"
    }
    $RemoteCommand = "set -eu`nrepo=$Repo`n$($Installs -join "`n")`nrm -f /tmp/greenhouse_failed_*.json-$Token"
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { throw "VPS failed-JSON sync failed" }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
