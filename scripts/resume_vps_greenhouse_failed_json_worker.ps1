param(
    [ValidateSet(
        "core-product-management",
        "growth-general-marketing",
        "product-marketing-gtm",
        "program-project-management",
        "technical-ai-platform-product-management"
    )]
    [string]$Worker,
    [switch]$RequeueClarification,
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-resume"
$Unit = "job-app-greenhouse-failed-$Worker.service"
$StateSuffix = $Worker.Replace("-", "_")
$RequeueCommand = if ($RequeueClarification) {
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
PYTHONPATH='/root/Job-App-Automation/src' python3 - '/root/Job-App-Automation/output/continuous_greenhouse_failed_${StateSuffix}_state.json' '/root/Job-App-Automation/output/continuous_greenhouse_failed_claims.json' <<'PY'
import json
import sys
from pathlib import Path

from job_application_automation.core.artifacts import atomic_write_text, interprocess_file_lock

state_path, claims_path = map(Path, sys.argv[1:])
state = json.loads(state_path.read_text(encoding="utf-8"))
candidates = []
for key, record in state.get("jobs", {}).items():
    if not isinstance(record, dict):
        continue
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    status = record.get("result_status") or result.get("status")
    if status == "REQUIRED_FIELDS_NOT_FILLED":
        candidates.append((str(record.get("updated_at", "")), key, record))
if not candidates:
    raise SystemExit("no clarification failure is available to requeue")
_, key, record = max(candidates)
job_url = str(record.get("job_url", key))
del state["jobs"][key]
atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
with interprocess_file_lock(claims_path):
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    for identity, claim in list(claims.get("jobs", {}).items()):
        if isinstance(claim, dict) and claim.get("job_url") == job_url:
            del claims["jobs"][identity]
    atomic_write_text(claims_path, json.dumps(claims, indent=2, sort_keys=True) + "\n")
print(f"requeued_clarification={job_url}")
PY
"@
} else { "" }
$RemoteCommand = "set -eu`n$RequeueCommand`nsystemctl reset-failed '$Unit'; systemctl start '$Unit'; systemctl show '$Unit' --property=Id,ActiveState,SubState,NRestarts,MainPID"

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
