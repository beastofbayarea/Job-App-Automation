param(
    [ValidateSet("all", "marketing", "product-management")]
    [string]$Worker = "all",
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(60, 1800)]
    [int]$TimeoutSeconds = 900
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"

$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$WorkerLiteral = ConvertTo-PosixShellLiteral $Worker
$StatePath = ConvertTo-PosixShellLiteral (
    "$($RemoteRepoPath.TrimEnd('/'))/output/continuous_greenhouse_excel_${Worker}_state.json"
)
$WorkbookName = switch ($Worker) {
    "all" { "greenhouse_all_jobs.xlsx" }
    "marketing" { "greenhouse_marketing_jobs.xlsx" }
    "product-management" { "greenhouse_product_management_jobs.xlsx" }
}
$TrackerPath = ConvertTo-PosixShellLiteral (
    "$($RemoteRepoPath.TrimEnd('/'))/data/$WorkbookName"
)
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "greenhouse-tracker-retry"

$RemoteCommand = @"
set -eu
repo=$Repo
worker=$WorkerLiteral
state=$StatePath
tracker=$TrackerPath
retry_root=`$(mktemp -d "`$repo/output/greenhouse-tracker-retry.XXXXXXXX")
services='job-app-greenhouse.service job-app-greenhouse-excel-all.service job-app-greenhouse-excel-marketing.service job-app-greenhouse-excel-product-management.service'

if pgrep -f '[j]ob_automation.py (apply|documents generate)' >/dev/null; then
  printf '%s\n' 'An application or document-generation process is active; refusing overlap.' >&2
  exit 76
fi

PYTHONPATH="`$repo/src" "`$repo/.venv/bin/python" - \
  "`$state" "`$tracker" "`$repo/output/submission_log.json" "`$retry_root/tracker.xlsx" <<'PY'
import json
import sys
from pathlib import Path

import openpyxl

from job_application_automation.core.orchestrator import load_jobs_from_tracker

state_path, tracker_path, ledger_path, retry_path = map(Path, sys.argv[1:])
state = json.loads(state_path.read_text(encoding="utf-8"))
candidates = []
for record in state.get("jobs", {}).values():
    if not isinstance(record, dict):
        continue
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    result_status = record.get("result_status") or result.get("status")
    if record.get("status") != "failed" or result_status != "REQUIRED_FIELDS_NOT_FILLED":
        continue
    if result.get("submitted") is True or result.get("confirmed") is True:
        continue
    candidates.append(record)
if not candidates:
    raise SystemExit("no verified pre-submit REQUIRED_FIELDS_NOT_FILLED record remains")
candidates.sort(key=lambda item: str(item.get("updated_at", "")))
record = candidates[0]
target_url = str(record.get("job_url", ""))
if not target_url:
    raise SystemExit("selected state record has no job URL")

ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
entries = ledger.values() if isinstance(ledger, dict) else ledger
if any(
    isinstance(item, dict)
    and item.get("status") == "SUBMITTED & CONFIRMED"
    and str(item.get("job_url", "")) == target_url
    for item in entries
):
    raise SystemExit("refusing retry: target already exists in confirmed ledger")

jobs = load_jobs_from_tracker(tracker_path)
job = next((item for item in jobs if str(item.get("url", "")) == target_url), None)
if job is None:
    raise SystemExit("selected failure is absent from its source workbook")

workbook = openpyxl.Workbook()
sheet = workbook.active
sheet.append(["Company", "Job Title", "Job URL"])
sheet.append([job["company"], job["role"], job["url"]])
workbook.save(retry_path)
workbook.close()
print("retry_safety=verified_pre_submit_failure")
print(f"retry_company={job['company']}")
print(f"retry_title={job['role']}")
print(f"retry_url={job['url']}")
PY

systemctl stop `$services
restore_workers() {
  systemctl start `$services
}
trap restore_workers EXIT INT TERM

set +e
PYTHONPATH="`$repo/src" xvfb-run -a --server-args="-screen 0 1280x1024x24" \
  "`$repo/.venv/bin/python" -m job_application_automation.core.continuous_source_ats \
  --ats-platform greenhouse --source tracker --worker-id targeted-retry --once \
  --tracker "`$retry_root/tracker.xlsx" --state "`$retry_root/state.json" \
  --claims "`$retry_root/claims.json" --selected-input "`$retry_root/selected.json" \
  --results-dir "`$retry_root/results" --documents-dir "`$retry_root/documents"
retry_exit=`$?
set -e

PYTHONPATH="`$repo/src" "`$repo/.venv/bin/python" - "`$retry_root/state.json" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
records = [item for item in payload.get("jobs", {}).values() if isinstance(item, dict)]
if len(records) != 1:
    raise SystemExit(f"expected one retry record, found {len(records)}")
record = records[0]
result = record.get("result") if isinstance(record.get("result"), dict) else {}
print("retry_result=" + json.dumps({
    "status": record.get("status"),
    "result_status": record.get("result_status"),
    "submitted": result.get("submitted"),
    "confirmed": result.get("confirmed"),
    "error": result.get("error") or result.get("detail"),
    "missing_required": result.get("missing_required", []),
}, ensure_ascii=False, sort_keys=True))
for name in ("stdout_tail", "stderr_tail"):
    text = str(record.get(name, ""))
    lines = [line for line in text.splitlines() if re.search(r"required|missing|question|field", line, re.I)]
    if lines:
        print(name + "=" + "\n".join(lines[-20:]))
PY
printf 'retry_exit=%s\n' "`$retry_exit"
"@
$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)", $RemoteCommand
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) {
        Write-Error "Targeted tracker retry failed with exit code $($Execution.ExitCode)."
        exit $Execution.ExitCode
    }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
