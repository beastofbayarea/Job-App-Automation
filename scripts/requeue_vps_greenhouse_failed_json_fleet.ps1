param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-fleet-requeue"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$Units = @(
    "job-app-greenhouse-failed-core-product-management.service",
    "job-app-greenhouse-failed-growth-general-marketing.service",
    "job-app-greenhouse-failed-product-marketing-gtm.service",
    "job-app-greenhouse-failed-program-project-management.service",
    "job-app-greenhouse-failed-technical-ai-platform-product-management.service"
)
$UnitNames = $Units -join " "
$RemoteCommand = @"
set -eu
repo=$Repo
test -s "`$repo/data/resumes/base-resume.txt"
systemctl stop $UnitNames
PYTHONPATH="`$repo/src" "`$repo/.venv/bin/python" - "`$repo/output" <<'PY'
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_application_automation.core.artifacts import atomic_write_text
from job_application_automation.core.identity import canonical_job_url

output = Path(sys.argv[1])
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup = output / "requeue_backups" / stamp
backup.mkdir(parents=True, exist_ok=False)
claims_path = output / "continuous_greenhouse_failed_claims.json"
ledger_path = output / "submission_log.json"
state_paths = sorted(output.glob("continuous_greenhouse_failed_*_state.json"))
for path in [claims_path, *state_paths]:
    if path.is_file():
        shutil.copy2(path, backup / path.name)

confirmed = set()
if ledger_path.is_file():
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    for record in ledger.values():
        if not isinstance(record, dict) or record.get("status") != "SUBMITTED & CONFIRMED":
            continue
        if str(record.get("ats", "")).lower() != "greenhouse":
            continue
        try:
            confirmed.add(canonical_job_url(str(record.get("job_url", ""))))
        except ValueError:
            pass

retryable = {
    "DOCUMENT_GENERATION_FAILED", "REQUIRED_FIELDS_NOT_FILLED", "TIMED_OUT",
    "JOB_CONTEXT_UNAVAILABLE", "SKIPPED_APPLICATION_POLICY",
}
claims = json.loads(claims_path.read_text(encoding="utf-8")) if claims_path.is_file() else {"jobs": {}}
claim_jobs = claims.setdefault("jobs", {})
total = 0
for state_path in state_paths:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    worker = state_path.stem.removeprefix("continuous_greenhouse_").removesuffix("_state").replace("_", "-")
    requeued = 0
    for key, record in list(state.get("jobs", {}).items()):
        if not isinstance(record, dict):
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        result_status = str(record.get("result_status") or result.get("status") or "")
        job_url = str(record.get("job_url") or key)
        try:
            canonical = canonical_job_url(job_url)
        except ValueError:
            continue
        if canonical in confirmed or result_status not in retryable:
            continue
        del state["jobs"][key]
        for claim in claim_jobs.values():
            if isinstance(claim, dict) and claim.get("job_url") == job_url:
                claim["status"] = "retry_requested"
                claim["owner"] = worker
        requeued += 1
    atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    total += requeued
    print(f"worker={worker} requeued={requeued}")
claims["updated_at"] = datetime.now(timezone.utc).isoformat()
atomic_write_text(claims_path, json.dumps(claims, indent=2, sort_keys=True) + "\n")
print(f"backup={backup}")
print(f"total_requeued={total}")
if total == 0:
    raise SystemExit("no safe retryable records were found")
PY
systemctl reset-failed $UnitNames || true
systemctl start $UnitNames
sleep 12
systemctl show $UnitNames --property=Id,ActiveState,SubState,NRestarts,ExecMainStartTimestamp
"@
try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { throw "Failed JSON fleet requeue failed with exit code $($Execution.ExitCode)" }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
