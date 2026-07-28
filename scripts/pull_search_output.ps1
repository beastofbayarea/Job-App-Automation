# Pulls the latest VPS search-run output files into the local output/ folder.
# Run from the repo root, or from anywhere:  pwsh scripts\pull_search_output.ps1
param(
    [string]$Branch = "vps-search-output"
)

$Files = @(
    "output/job_search_coverage.json",
    "output/ai_jobs.csv",
    "output/ats_boards_cache.json"
)

git fetch origin $Branch
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to fetch origin/$Branch"
    exit 1
}

# Check out each file individually rather than all at once: `git checkout` treats
# a multi-path checkout as atomic, so one missing file (e.g. the VPS hasn't
# produced it yet) would otherwise block every other file from being pulled.
$Pulled = @()
foreach ($File in $Files) {
    git checkout "origin/$Branch" -- $File 2>$null
    if ($LASTEXITCODE -eq 0) {
        $Pulled += $File
    } else {
        Write-Warning "Skipped $File (not present on origin/$Branch)"
    }
}

if ($Pulled.Count -eq 0) {
    Write-Error "No sync files found on origin/$Branch"
    exit 1
}

Write-Host "Pulled from origin/${Branch}: $($Pulled -join ', ')"
