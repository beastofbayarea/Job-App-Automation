param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [switch]$SummaryOnly,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-retry-audit"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$SummaryFlag = if ($SummaryOnly) { "1" } else { "0" }
$RemoteCommand = @"
set -eu
repo=$Repo
python3 - "`$repo/output" '$SummaryFlag' <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

output = Path(sys.argv[1])
summary_only = sys.argv[2] == "1"
claims = json.loads((output / "continuous_greenhouse_failed_claims.json").read_text(encoding="utf-8"))
status_counts = Counter()
retry_counts = Counter()
fixing_attempt_counts = Counter()
awaiting_remediation = 0
pending_by_owner = Counter()
pending = []
claimed = []
for claim in claims.get("jobs", {}).values():
    if not isinstance(claim, dict):
        continue
    status = str(claim.get("status") or "UNKNOWN")
    status_counts[status] += 1
    retry_counts[int(claim.get("retry_count") or 0)] += 1
    fixing_attempts = int(
        claim.get(
            "fixing_attempts",
            0,
        )
    )
    fixing_attempt_counts[fixing_attempts] += 1
    if status == "retry_requested" and claim.get("failure_revision"):
        awaiting_remediation += 1
    if status == "retry_requested":
        owner = str(claim.get("owner") or "UNKNOWN")
        pending_by_owner[owner] += 1
        pending.append({
            "owner": owner,
            "company": claim.get("company"),
            "title": claim.get("title"),
            "job_url": claim.get("job_url"),
        })
    elif status == "claimed":
        claimed.append({
            "owner": str(claim.get("owner") or "UNKNOWN"),
            "company": claim.get("company"),
            "title": claim.get("title"),
            "job_url": claim.get("job_url"),
        })
print(json.dumps({
    "claim_status_counts": dict(status_counts),
    "pending_retry_count": len(pending),
    "pending_by_owner": dict(pending_by_owner),
    "claimed_count": len(claimed),
    "retry_count_distribution": dict(sorted(retry_counts.items())),
    "fixing_attempt_count_distribution": dict(sorted(fixing_attempt_counts.items())),
    "awaiting_remediation_count": awaiting_remediation,
}, sort_keys=True))
if not summary_only:
    for item in claimed:
        print(json.dumps({"claimed": item}, sort_keys=True))
    for item in pending:
        print(json.dumps({"pending_retry": item}, sort_keys=True))
PY
"@
try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
