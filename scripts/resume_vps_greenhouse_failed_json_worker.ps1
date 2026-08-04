param(
    [ValidateSet(
        "core-product-management",
        "growth-general-marketing",
        "product-marketing-gtm",
        "program-project-management",
        "technical-ai-platform-product-management"
    )]
    [string]$Worker,
    [switch]$StopOnly,
    [switch]$RequeueClarification,
    [switch]$RequeueManualReview,
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
if (($RequeueClarification -and $RequeueManualReview) -or ($StopOnly -and ($RequeueClarification -or $RequeueManualReview))) {
    throw "Choose exactly one worker action: stop, requeue clarification, or requeue manual review."
}
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-resume"
$Unit = "job-app-greenhouse-failed-$Worker.service"
$StateSuffix = $Worker.Replace("-", "_")
$RequeueCommand = if ($RequeueClarification -or $RequeueManualReview) {
$RetryKind = if ($RequeueManualReview) { "manual-review" } else { "clarification" }
@"
python3 - '/root/Job-App-Automation/output/continuous_greenhouse_failed_${StateSuffix}_state.json' <<'PY'
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if any(
    isinstance(record, dict) and record.get("status") in {"preparing", "application_started"}
    for record in state.get("jobs", {}).values()
):
    raise SystemExit("worker has an in-flight application; refusing to interrupt it")
PY
systemctl stop '$Unit'
PYTHONPATH='/root/Job-App-Automation/src' python3 - '/root/Job-App-Automation/output/continuous_greenhouse_failed_${StateSuffix}_state.json' '/root/Job-App-Automation/output/continuous_greenhouse_failed_claims.json' '$RetryKind' <<'PY'
import json
import sys
from pathlib import Path

from job_application_automation.core.artifacts import atomic_write_text, interprocess_file_lock
from job_application_automation.core.continuous_source_ats import _job_identity

def greenhouse_identity(job_url):
    try:
        return _job_identity(str(job_url or ""), "greenhouse")
    except ValueError:
        return ""

state_path, claims_path = map(Path, sys.argv[1:3])
retry_kind = sys.argv[3]
state = json.loads(state_path.read_text(encoding="utf-8"))
candidates = []
for key, record in state.get("jobs", {}).items():
    if not isinstance(record, dict):
        continue
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    status = record.get("result_status") or result.get("status")
    matches = (
        status == "REQUIRED_FIELDS_NOT_FILLED"
        if retry_kind == "clarification"
        else record.get("status") == "manual_review"
    )
    if matches:
        candidates.append((str(record.get("updated_at", "")), key, record))
if not candidates:
    raise SystemExit(f"no {retry_kind} outcome is available to requeue")
_, key, record = max(candidates)
job_url = str(record.get("job_url", key))
job_identity = greenhouse_identity(job_url)
if record.get("retry_policy_status") == "skipped_after_fixing_attempts":
    raise SystemExit("refusing exhausted state after two fixing attempts")
with interprocess_file_lock(claims_path):
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    matched_claim = False
    for identity, claim in list(claims.get("jobs", {}).items()):
        if (
            isinstance(claim, dict)
            and greenhouse_identity(claim.get("job_url")) == job_identity
        ):
            fixing_attempts = int(
                claim.get(
                    "fixing_attempts",
                    max(int(claim.get("retry_count") or 0) - 1, 0),
                )
            )
            if claim.get("status") == "skipped_after_fixing_attempts" or fixing_attempts >= 2:
                raise SystemExit("refusing exhausted claim after two fixing attempts")
            claim["status"] = "retry_requested"
            matched_claim = True
    if not matched_claim:
        raise SystemExit("retry claim was not found")
    atomic_write_text(claims_path, json.dumps(claims, indent=2, sort_keys=True) + "\n")
del state["jobs"][key]
atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
print(f"requeued_{retry_kind}={job_url}")
PY
"@
} else { "" }
$RemoteCommand = if ($StopOnly) {
    "set -eu`nsystemctl stop '$Unit'; systemctl show '$Unit' --property=Id,ActiveState,SubState,NRestarts,MainPID"
} else {
    "set -eu`n$RequeueCommand`nsystemctl reset-failed '$Unit'; systemctl start '$Unit'; systemctl show '$Unit' --property=Id,ActiveState,SubState,NRestarts,MainPID"
}

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)", $RemoteCommand
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
