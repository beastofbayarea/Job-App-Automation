# scripts/prune_old_outputs.ps1
# Lists (or deletes, with -Delete) generated resume/cover-letter PDFs older than -Days.
param(
    [ValidateRange(0, [int]::MaxValue)]
    [int]$Days = 14,
    [switch]$Delete,
    [string]$OutputDir = "output"
)

if (-not (Test-Path -LiteralPath $OutputDir -PathType Container)) {
    Write-Error "Output directory not found: $OutputDir"
    exit 1
}

$Cutoff = (Get-Date).AddDays(-$Days)
$Patterns = @("*_Resume.pdf", "*_Cover_Letter.pdf")

$Candidates = foreach ($Pattern in $Patterns) {
    Get-ChildItem -LiteralPath $OutputDir -Filter $Pattern -File -ErrorAction Stop |
        Where-Object { $_.LastWriteTime -lt $Cutoff }
}

if (-not $Candidates) {
    Write-Host "No files older than $Days days found in $OutputDir."
    exit 0
}

$TotalBytes = ($Candidates | Measure-Object -Property Length -Sum).Sum
$TotalMB = [Math]::Round($TotalBytes / 1MB, 2)

if ($Delete) {
    $Candidates | Remove-Item -Force
    Write-Host "Deleted $($Candidates.Count) file(s), freed ${TotalMB}MB."
} else {
    Write-Host "DRY RUN: $($Candidates.Count) file(s) older than $Days days (${TotalMB}MB total). Re-run with -Delete to remove."
    $Candidates | ForEach-Object { Write-Host "  $($_.Name) ($($_.LastWriteTime))" }
}
