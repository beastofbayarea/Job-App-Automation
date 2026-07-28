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

git checkout "origin/$Branch" -- $Files
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to check out sync files from origin/$Branch"
    exit 1
}

Write-Host "Pulled latest VPS search output into output/ from origin/$Branch"
