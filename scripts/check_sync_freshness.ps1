# scripts/check_sync_freshness.ps1
# Reports whether the local coverage report reflects a recent VPS sync.
param(
    [string]$Path = "output/job_search_coverage.json",
    [int]$ThresholdHours = 24
)

if (-not (Test-Path $Path)) {
    Write-Warning "STALE: $Path not found. Run scripts\pull_search_output.ps1 first."
    exit 1
}

try {
    $Report = Get-Content $Path -Raw | ConvertFrom-Json -AsHashtable
} catch {
    Write-Error "STALE: $Path is not valid JSON."
    exit 1
}

if (-not $Report['generated_at']) {
    Write-Error "STALE: $Path has no 'generated_at' field."
    exit 1
}

$Generated = [DateTimeOffset]::Parse(($Report['generated_at']).ToString("o"))
$AgeHours = [Math]::Round(([DateTimeOffset]::UtcNow - $Generated).TotalHours, 1)

if ($AgeHours -gt $ThresholdHours) {
    Write-Warning "STALE (age: ${AgeHours}h, threshold: ${ThresholdHours}h)"
    exit 1
} else {
    Write-Host "OK (age: ${AgeHours}h)"
    exit 0
}
