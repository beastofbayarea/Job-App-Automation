# scripts/check_sync_freshness.ps1
# Reports whether the local coverage report reflects a recent VPS sync.
param(
    [string]$Path = "output/job_search_coverage.json",
    [ValidateRange(0, [int]::MaxValue)]
    [int]$ThresholdHours = 24,
    [ValidateRange(0, [int]::MaxValue)]
    [int]$ClockSkewMinutes = 5
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

try {
    $GeneratedValue = $Report['generated_at']
    if ($GeneratedValue -is [DateTimeOffset]) {
        $Generated = $GeneratedValue
    } elseif ($GeneratedValue -is [DateTime]) {
        $Generated = [DateTimeOffset]$GeneratedValue
    } else {
        $Generated = [DateTimeOffset]::Parse(
            [string]$GeneratedValue,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
    }
} catch {
    Write-Error "STALE: $Path has an invalid 'generated_at' timestamp."
    exit 1
}

$AgeHours = ([DateTimeOffset]::UtcNow - $Generated).TotalHours
$DisplayAgeHours = [Math]::Round($AgeHours, 1)

if ($AgeHours -lt -($ClockSkewMinutes / 60)) {
    Write-Error "STALE: generated_at is more than ${ClockSkewMinutes}m in the future."
    exit 1
} elseif ($AgeHours -gt $ThresholdHours) {
    Write-Warning "STALE (age: ${DisplayAgeHours}h, threshold: ${ThresholdHours}h)"
    exit 1
} else {
    Write-Host "OK (age: ${DisplayAgeHours}h)"
    exit 0
}
