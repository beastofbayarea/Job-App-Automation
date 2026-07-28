# Pulls the latest VPS search-run output files into the local output/ folder.
# Run from the repo root, or from anywhere:  pwsh scripts\pull_search_output.ps1
param(
    [string]$Branch = "vps-search-output",
    [string]$RepositoryPath = (Join-Path $PSScriptRoot "..")
)

$Files = @(
    "output/job_search_coverage.json",
    "output/ai_jobs.csv",
    "output/ats_boards_cache.json"
)

try {
    $RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryPath -ErrorAction Stop).Path
} catch {
    Write-Error "Repository path not found: $RepositoryPath"
    exit 1
}

git check-ref-format --branch $Branch *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Invalid sync branch name: $Branch"
    exit 1
}

git -C $RepositoryRoot rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Not a Git worktree: $RepositoryRoot"
    exit 1
}

git -C $RepositoryRoot fetch origin $Branch
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to fetch origin/$Branch"
    exit 1
}

$RemoteRef = "refs/remotes/origin/$Branch"
$Commit = (git -C $RepositoryRoot rev-parse --verify "$RemoteRef^{commit}" 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Commit) {
    Write-Error "Unable to resolve origin/$Branch after fetching it."
    exit 1
}

$MissingFiles = @()
foreach ($File in $Files) {
    git -C $RepositoryRoot cat-file -e "${Commit}:$File" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $MissingFiles += $File
    }
}

if ($MissingFiles.Count -gt 0) {
    Write-Error "Sync commit $($Commit.Substring(0, 12)) is incomplete; missing required files: $($MissingFiles -join ', ')"
    exit 1
}

# Restore the complete snapshot from one resolved commit without touching the
# current branch or index. Every file is verified first, so a partial remote
# snapshot cannot replace a coherent local one.
git -C $RepositoryRoot restore "--source=$Commit" --worktree -- $Files
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to restore the sync snapshot from commit $($Commit.Substring(0, 12))."
    exit 1
}

Write-Host "Pulled commit $($Commit.Substring(0, 12)) from origin/${Branch}: $($Files -join ', ')"
