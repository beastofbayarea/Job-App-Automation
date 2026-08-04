[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$Path,

    [ValidateRange(1, 24)]
    [int]$MaximumAgeMonths = 2,

    [datetime]$AsOf = (Get-Date),

    [switch]$VerifyLive
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
$cutoff = $AsOf.Date.AddMonths(-$MaximumAgeMonths)
$uuidPattern = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
$boardCache = @{}

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
        $errors.Add("Posting predates the $MaximumAgeMonths-month calendar window: '$($record.company)'")
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
    if (-not $validUri -or
        $uri.Scheme -ne "https" -or
        $uri.Host -ne "jobs.ashbyhq.com" -or
        $uri.Query -or
        $uri.Fragment -or
        $parts.Count -ne 2 -or
        $parts[1] -notmatch $uuidPattern) {
        $errors.Add("Invalid canonical Ashby posting URL: '$($record.url)'")
        continue
    }

    if ($VerifyLive) {
        $board = $parts[0]
        if (-not $boardCache.ContainsKey($board)) {
            try {
                $boardCache[$board] = Invoke-RestMethod `
                    -Uri "https://api.ashbyhq.com/posting-api/job-board/$board" `
                    -Headers @{ Accept = "application/json" } `
                    -TimeoutSec 30
            }
            catch {
                $boardCache[$board] = $null
                $errors.Add("Board verification failed for '$board': $($_.Exception.Message)")
            }
        }
        $payload = $boardCache[$board]
        if ($null -eq $payload) {
            continue
        }
        $matches = @($payload.jobs | Where-Object {
            [string]$_.id -eq $parts[1] -and $_.isListed -ne $false
        })
        if ($matches.Count -ne 1) {
            $errors.Add("Posting is absent or unlisted for '$($record.company)'")
            continue
        }
        $posting = $matches[0]
        if ([string]$posting.title.Trim() -ne [string]$record.title.Trim()) {
            $errors.Add("Live title mismatch for '$($record.company)'")
        }
        $publishedDate = ([datetime]$posting.publishedAt).ToUniversalTime().Date
        if ($publishedDate -ne $postingDate.Date) {
            $errors.Add("publishedAt mismatch for '$($record.company)'")
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

$suffix = if ($VerifyLive) { " with live board API verification" } else { "" }
Write-Output "Validated $($records.Count) Ashby jobs$suffix in '$Path'."
