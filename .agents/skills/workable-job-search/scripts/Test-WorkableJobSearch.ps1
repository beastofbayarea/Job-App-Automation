[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$Path,

    [ValidateRange(1, 3650)]
    [int]$MaximumAgeDays = 62,

    [datetime]$AsOf = (Get-Date)
)

$records = @(Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json)
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
    if (-not [uri]::TryCreate([string]$record.url, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "https" -or
        $uri.Host -ne "apply.workable.com" -or
        $uri.Query -or
        $uri.Fragment -or
        $uri.AbsolutePath -notmatch "^/(?:[^/]+/)?j/[A-Za-z0-9]+/?$") {
        $errors.Add("Invalid canonical Workable posting URL: '$($record.url)'")
    }
}

if (-not $records.Count) {
    $errors.Add("The result contains no jobs")
}
if ($errors.Count) {
    $errors | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Output "Validated $($records.Count) Workable jobs in '$Path'."
