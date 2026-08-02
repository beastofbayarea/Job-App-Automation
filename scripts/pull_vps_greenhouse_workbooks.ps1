param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$DestinationPath = "data"
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PscpPath = Get-RequiredCommandPath -Name "pscp"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "greenhouse-workbooks"
$Destination = [IO.Path]::GetFullPath((Join-Path (Get-Location) $DestinationPath))
$Workspace = [IO.Path]::GetFullPath((Get-Location))
if ($Destination -ne $Workspace -and -not $Destination.StartsWith($Workspace + [IO.Path]::DirectorySeparatorChar)) {
    throw "DestinationPath must remain within the current workspace."
}
[IO.Directory]::CreateDirectory($Destination) | Out-Null
$Names = @(
    "greenhouse_all_jobs.xlsx",
    "greenhouse_marketing_jobs.xlsx",
    "greenhouse_product_management_jobs.xlsx"
)
$Staged = [Collections.Generic.List[string]]::new()

try {
    foreach ($Name in $Names) {
        $Temporary = Join-Path $Destination ".$Name.$([guid]::NewGuid().ToString('N')).tmp"
        $Staged.Add($Temporary)
        $Remote = "$($Connection.User)@$($Connection.Host):$($RemoteRepoPath.TrimEnd('/'))/data/$Name"
        & $PscpPath -batch -P $Connection.Port -hostkey $Connection.HostKey -pwfile $PasswordFile `
            $Remote $Temporary
        if ($LASTEXITCODE -ne 0) { throw "Failed to download $Name from the VPS." }
        $Header = [IO.File]::ReadAllBytes($Temporary) | Select-Object -First 4
        if ($Header.Count -ne 4 -or $Header[0] -ne 0x50 -or $Header[1] -ne 0x4B) {
            throw "Downloaded file is not a valid XLSX container: $Name"
        }
    }
    for ($Index = 0; $Index -lt $Names.Count; $Index++) {
        Move-Item -LiteralPath $Staged[$Index] -Destination (Join-Path $Destination $Names[$Index]) -Force
    }
} finally {
    foreach ($Temporary in $Staged) {
        Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}

Write-Host "Downloaded and validated Greenhouse workbooks: $($Names -join ', ')"
