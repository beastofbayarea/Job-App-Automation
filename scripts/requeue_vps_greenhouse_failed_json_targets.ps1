param(
    [Parameter(Mandatory)]
    [string[]]$Target,
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"

$AllowedWorkers = @(
    "core-product-management",
    "growth-general-marketing",
    "product-marketing-gtm",
    "program-project-management",
    "technical-ai-platform-product-management"
)
if (-not $Target -or $Target.Count -eq 0) {
    throw "At least one worker|company|title target is required."
}
$ValidatedTargets = @($Target | ForEach-Object {
    $Parts = @($_ -split '\|', 3)
    if ($Parts.Count -ne 3 -or @($Parts | Where-Object { -not $_.Trim() }).Count -gt 0) {
        throw "Invalid target '$_'; expected worker|company|title."
    }
    $Worker = $Parts[0].Trim().ToLowerInvariant()
    if ($AllowedWorkers -notcontains $Worker) {
        throw "Unsupported failed-JSON worker '$Worker'."
    }
    "$Worker|$($Parts[1].Trim())|$($Parts[2].Trim())"
})

$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-targets"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$TargetPayload = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes(($ValidatedTargets | ConvertTo-Json -Compress))
)
$TargetWorkers = @($ValidatedTargets | ForEach-Object {
    ($_ -split '\|', 2)[0]
} | Sort-Object -Unique)
$UnitNames = @($TargetWorkers | ForEach-Object {
    "job-app-greenhouse-failed-$_.service"
}) -join " "
$RemoteCommand = @"
set -eu
repo=$Repo
systemctl stop $UnitNames
trap 'systemctl start $UnitNames' EXIT
PYTHONPATH="`$repo/src" "`$repo/.venv/bin/python" - "`$repo/output" '$TargetPayload' <<'PY'
import base64
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_application_automation.core.artifacts import atomic_write_text
from job_application_automation.core.continuous_source_ats import _job_identity
from job_application_automation.core.continuous_worker_candidates import (
    load_exact_confirmed_ledger_index,
)

ALLOWED_WORKERS = {
    "core-product-management",
    "growth-general-marketing",
    "product-marketing-gtm",
    "program-project-management",
    "technical-ai-platform-product-management",
}
SAFE_PRE_SUBMIT_RESULT_STATUSES = {
    "DOCUMENT_GENERATION_FAILED",
    "JOB_CONTEXT_UNAVAILABLE",
    "REQUIRED_FIELDS_NOT_FILLED",
}


def greenhouse_identity(job_url):
    try:
        return _job_identity(str(job_url or ""), "greenhouse")
    except ValueError:
        return ""


output = Path(sys.argv[1])
raw_targets = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
if isinstance(raw_targets, str):
    raw_targets = [raw_targets]
wanted = set()
for target in raw_targets:
    parts = str(target).split("|", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise SystemExit(f"invalid target {target!r}; expected worker|company|title")
    worker, company, title = (part.strip() for part in parts)
    worker = worker.casefold()
    if worker not in ALLOWED_WORKERS:
        raise SystemExit(f"unsupported failed-JSON worker: {worker}")
    wanted.add((worker, company.casefold(), title.casefold()))

claims_path = output / "continuous_greenhouse_failed_claims.json"
ledger_path = output / "submission_log.json"
if not claims_path.is_file():
    raise SystemExit(f"claims file not found: {claims_path}")
if not ledger_path.is_file():
    raise SystemExit(f"confirmed ledger not found: {ledger_path}")

claims = json.loads(claims_path.read_text(encoding="utf-8"))
claim_jobs = claims.get("jobs")
if not isinstance(claim_jobs, dict):
    raise SystemExit("claims jobs must be an object")
confirmed = load_exact_confirmed_ledger_index(
    ledger_path,
    "greenhouse",
    identity_for_url=lambda job_url: _job_identity(job_url, "greenhouse"),
)

state_payloads = {}
for worker, _company, _title in wanted:
    path = output / f"continuous_greenhouse_failed_{worker.replace('-', '_')}_state.json"
    if not path.is_file():
        raise SystemExit(f"target worker state not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
        raise SystemExit(f"invalid target worker state: {path}")
    state_payloads[path] = payload

planned = []
matched_targets = set()
planned_records = set()
for path, state in state_payloads.items():
    worker = path.stem.removeprefix("continuous_greenhouse_failed_").removesuffix("_state")
    worker = worker.replace("_", "-").casefold()
    for key, record in state["jobs"].items():
        if not isinstance(record, dict):
            continue
        target_key = (
            worker,
            str(record.get("company", "")).strip().casefold(),
            str(record.get("title", "")).strip().casefold(),
        )
        if target_key not in wanted:
            continue
        matched_targets.add(target_key)
        record_key = (path, key)
        if record_key in planned_records:
            continue
        planned_records.add(record_key)

        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        result_status = str(
            record.get("result_status") or result.get("status") or record.get("status") or ""
        )
        record_status = str(record.get("status") or "")
        job_url = str(record.get("job_url") or key)
        identity = greenhouse_identity(job_url)
        if not identity:
            raise SystemExit(f"target has no valid Greenhouse identity: {target_key}")
        if confirmed.contains(identity):
            raise SystemExit(f"refusing target already present in exact confirmed ledger: {target_key}")
        if record.get("ledger_confirmed") is True or result_status == "SUBMITTED & CONFIRMED":
            raise SystemExit(f"refusing target with confirmation evidence: {target_key}")
        if record_status != "failed":
            raise SystemExit(
                f"refusing target without a terminal failed state ({record_status}): {target_key}"
            )
        if result_status not in SAFE_PRE_SUBMIT_RESULT_STATUSES:
            raise SystemExit(
                f"refusing target without an allowlisted pre-submit result ({result_status}): "
                f"{target_key}"
            )
        if result.get("submitted") is not False or result.get("confirmed") is True:
            raise SystemExit(
                "refusing target without verified pre-submit failure evidence "
                f"(submitted=False, confirmed!=True): {target_key}"
            )
        if record.get("retry_policy_status") == "skipped_after_fixing_attempts":
            raise SystemExit(f"refusing exhausted target after two fixing attempts: {target_key}")

        claim = claim_jobs.get(identity)
        if not isinstance(claim, dict):
            raise SystemExit(f"retry claim was not found for target: {target_key}")
        if greenhouse_identity(claim.get("job_url")) != identity:
            raise SystemExit(f"retry claim identity mismatch for target: {target_key}")
        if claim.get("status") == "confirmed":
            raise SystemExit(f"refusing confirmed claim for target: {target_key}")
        if (
            claim.get("status") == "skipped_after_fixing_attempts"
            or int(claim.get("fixing_attempts", 0)) >= 2
        ):
            raise SystemExit(f"refusing exhausted claim after two fixing attempts: {target_key}")
        planned.append((path, key, worker, identity, result_status))

missing = sorted("|".join(item) for item in wanted - matched_targets)
if missing:
    raise SystemExit(f"targets not found: {missing}")
if not planned:
    raise SystemExit("no verified pre-submit targets were available to authorize")

stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
backup = output / "requeue_backups" / f"targeted-{stamp}"
backup.mkdir(parents=True, exist_ok=False)
changed_paths = [*sorted(state_payloads), claims_path]
original_text = {path: path.read_text(encoding="utf-8") for path in changed_paths}
for path in changed_paths:
    shutil.copy2(path, backup / path.name)

now = datetime.now(timezone.utc).isoformat()
authorized = []
for path, key, worker, identity, result_status in planned:
    del state_payloads[path]["jobs"][key]
    claim = claim_jobs[identity]
    claim.update(
        {
            "status": "retry_requested",
            "owner": f"failed-{worker}",
            "retry_authorized": True,
            "remediation_required": False,
            "updated_at": now,
        }
    )
    claim.pop("failure_revision", None)
    claim.pop("next_retry_at", None)
    claim.pop("skip_reason", None)
    authorized.append((worker, identity, result_status))
claims["updated_at"] = now

try:
    # State is removed first. If the claims write fails, the old unauthorized
    # claim still blocks execution until this transaction is restored.
    for path in sorted(state_payloads):
        atomic_write_text(path, json.dumps(state_payloads[path], indent=2, sort_keys=True) + "\n")
    atomic_write_text(claims_path, json.dumps(claims, indent=2, sort_keys=True) + "\n")
except Exception:
    for path, text in original_text.items():
        atomic_write_text(path, text)
    raise

print(f"backup={backup}")
for worker, identity, result_status in sorted(authorized):
    print(f"retry_authorized={worker}|{identity}|{result_status}")
PY
systemctl reset-failed $UnitNames || true
systemctl start $UnitNames
sleep 5
systemctl show $UnitNames --property=Id,ActiveState,SubState,NRestarts,ExecMainStartTimestamp
trap - EXIT
"@
try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) {
        throw "Targeted failed JSON requeue failed with exit code $($Execution.ExitCode)"
    }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
