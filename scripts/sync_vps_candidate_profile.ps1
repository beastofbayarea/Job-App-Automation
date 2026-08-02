param(
    [string]$ProfilePath = "config/candidate_profile_config.json",
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { throw "Profile not found: $ProfilePath" }
Get-Content -LiteralPath $ProfilePath -Raw | ConvertFrom-Json | Out-Null
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PscpPath = Get-RequiredCommandPath -Name "pscp"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$Token = [guid]::NewGuid().ToString("N")
$RemoteStage = "/tmp/candidate-profile-$Token.json"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "candidate-profile-sync"

try {
    & $PscpPath -batch -P $Connection.Port -hostkey $Connection.HostKey -pwfile $PasswordFile `
        $ProfilePath "$($Connection.User)@$($Connection.Host):$RemoteStage"
    if ($LASTEXITCODE -ne 0) { throw "Candidate profile upload failed with exit code $LASTEXITCODE" }
    $RemoteCommand = "set -eu; python3 -m json.tool '$RemoteStage' >/dev/null; install -m 0600 '$RemoteStage' $Repo/config/candidate_profile_config.json; rm -f '$RemoteStage'"
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { throw "Candidate profile install failed with exit code $($Execution.ExitCode)" }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
