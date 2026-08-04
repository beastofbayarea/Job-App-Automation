[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$Path,

    [ValidateRange(1, 3650)]
    [int]$MaximumAgeDays = 62,

    [datetime]$AsOf = (Get-Date),

    [switch]$VerifyLive
)

$parsed = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
$records = if ($parsed -is [System.Array]) { @($parsed.GetEnumerator()) } else { @($parsed) }
$errors = [System.Collections.Generic.List[string]]::new()
$required = @("posting_date", "company", "title", "location", "url")
$seenCompanies = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$seenUrls = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$previousDate = [datetime]::MaxValue
$cutoff = $AsOf.Date.AddDays(-$MaximumAgeDays)
$uuidPattern = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

foreach ($record in $records) {
    $properties = @($record.PSObject.Properties.Name)
    $missing = @($required | Where-Object { $_ -notin $properties })
    $extra = @($properties | Where-Object { $_ -notin $required })
    if ($missing.Count -or $extra.Count) {
        $errors.Add("Invalid schema for '$($record.company)': missing=[$($missing -join ', ')], extra=[$($extra -join ', ')]")
        continue
    }

    $postingDate = [datetime]::MinValue
    if (-not [datetime]::TryParseExact(
        [string]$record.posting_date,
        "yyyy-MM-dd",
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None,
        [ref]$postingDate
    )) {
        $errors.Add("Invalid posting_date for '$($record.company)'")
    }
    elseif ($postingDate -lt $cutoff) {
        $errors.Add("Posting is older than $MaximumAgeDays days: '$($record.company)'")
    }
    elseif ($postingDate -gt $previousDate) {
        $errors.Add("Records are not sorted newest to oldest")
    }
    else {
        $previousDate = $postingDate
    }

    if (-not $seenCompanies.Add([string]$record.company)) {
        $errors.Add("Duplicate company: '$($record.company)'")
    }
    if (-not $seenUrls.Add([string]$record.url)) {
        $errors.Add("Duplicate URL: '$($record.url)'")
    }

    $uri = $null
    $validUri = [uri]::TryCreate([string]$record.url, [UriKind]::Absolute, [ref]$uri)
    $parts = if ($validUri) {
        @($uri.AbsolutePath.Split("/", [StringSplitOptions]::RemoveEmptyEntries))
    } else {
        @()
    }
    $allowedHosts = @("jobs.lever.co", "jobs.eu.lever.co")
    if (-not $validUri -or
        $uri.Scheme -ne "https" -or
        $uri.Host -notin $allowedHosts -or
        $uri.Query -or
        $uri.Fragment -or
        $parts.Count -ne 2 -or
        $parts[1] -notmatch $uuidPattern) {
        $errors.Add("Invalid canonical Lever posting URL: '$($record.url)'")
        continue
    }

    if ($VerifyLive) {
        $apiHost = if ($uri.Host -eq "jobs.eu.lever.co") {
            "api.eu.lever.co"
        } else {
            "api.lever.co"
        }
        $apiUrl = "https://$apiHost/v0/postings/$($parts[0])/$($parts[1])?mode=json"
        try {
            $posting = Invoke-RestMethod -Uri $apiUrl -Headers @{
                Accept = "application/json"
            } -TimeoutSec 30
        }
        catch {
            $errors.Add("Live verification failed for '$($record.company)': $($_.Exception.Message)")
            continue
        }
        if ([string]$posting.id -ne $parts[1]) {
            $errors.Add("Posting ID mismatch for '$($record.company)'")
        }
        if ([string]$posting.text -ne [string]$record.title) {
            $errors.Add("Live title mismatch for '$($record.company)'")
        }
        $createdDate = [DateTimeOffset]::FromUnixTimeMilliseconds(
            [int64]$posting.createdAt
        ).UtcDateTime.Date
        if ($createdDate -ne $postingDate.Date) {
            $errors.Add("createdAt mismatch for '$($record.company)'")
        }
    }
}

if (-not $records.Count) {
    $errors.Add("The result contains no jobs")
}
if ($errors.Count) {
    $errors | ForEach-Object { Write-Error $_ }
    exit 1
}

$suffix = if ($VerifyLive) { " with live API verification" } else { "" }
Write-Output "Validated $($records.Count) Lever jobs$suffix in '$Path'."
