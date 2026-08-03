param(
    [string]$EvidencePath = "data/resumes/base-resume.txt",
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
if (-not (Test-Path -LiteralPath $EvidencePath -PathType Leaf)) { throw "Evidence file not found: $EvidencePath" }
if ((Get-Item -LiteralPath $EvidencePath).Length -eq 0) { throw "Evidence file is empty: $EvidencePath" }
$Content = Get-Content -LiteralPath $EvidencePath -Raw
foreach ($Marker in @("[NAME]", "[LOCATION]", "SOURCE RULES")) {
    if (-not $Content.Contains($Marker)) { throw "Evidence file is missing required marker: $Marker" }
}

$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PscpPath = Get-RequiredCommandPath -Name "pscp"
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "candidate-evidence-sync"
$Token = [guid]::NewGuid().ToString("N")
$RemoteStage = "/tmp/base-resume-$Token.txt"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
try {
    & $PscpPath -batch -P $Connection.Port -hostkey $Connection.HostKey -pwfile $PasswordFile `
        $EvidencePath "$($Connection.User)@$($Connection.Host):$RemoteStage"
    if ($LASTEXITCODE -ne 0) { throw "Candidate evidence upload failed with exit code $LASTEXITCODE" }
    $RemoteCommand = "set -eu; install -d -m 0700 $Repo/data/resumes; install -m 0600 '$RemoteStage' $Repo/data/resumes/base-resume.txt; rm -f '$RemoteStage'; test -s $Repo/data/resumes/base-resume.txt; wc -c $Repo/data/resumes/base-resume.txt"
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)", $RemoteCommand
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { throw "Candidate evidence install failed with exit code $($Execution.ExitCode)" }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
