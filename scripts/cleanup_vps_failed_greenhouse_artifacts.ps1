param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$OutputJsonPath = "output/greenhouse_failed_unsubmitted_cleanup.json",
    [ValidateRange(30, 600)]
    [int]$TimeoutSeconds = 300
)

. "$PSScriptRoot\lib\vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PscpPath = Get-RequiredCommandPath -Name "pscp"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$Token = [guid]::NewGuid().ToString("N")
$RemoteReport = "/tmp/greenhouse-failed-unsubmitted-$Token.json"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "greenhouse-cleanup"
$OutputJsonPath = [IO.Path]::GetFullPath((Join-Path (Get-Location) $OutputJsonPath))
$OutputDirectory = Split-Path -Parent $OutputJsonPath
if (-not (Test-Path -LiteralPath $OutputDirectory)) {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}

$RemoteCommand = @"
set -eu
repo=$Repo
PYTHONPATH="`$repo/src" "`$repo/.venv/bin/python" - "`$repo" '$RemoteReport' <<'PY'
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_application_automation.core.identity import canonical_job_url

repo = Path(sys.argv[1]).resolve()
report_path = Path(sys.argv[2])
output = (repo / "output").resolve()

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default

def records(payload):
    jobs = payload.get("jobs", {}) if isinstance(payload, dict) else {}
    return jobs.values() if isinstance(jobs, dict) else ()

def canonical(raw):
    try:
        return canonical_job_url(str(raw or ""))
    except ValueError:
        return ""

ledger = load_json(output / "submission_log.json", {})
ledger_entries = ledger.values() if isinstance(ledger, dict) else ledger
confirmed_urls = {
    canonical(item.get("job_url") or item.get("url"))
    for item in ledger_entries
    if isinstance(item, dict)
    and item.get("status") == "SUBMITTED & CONFIRMED"
    and str(item.get("ats") or item.get("platform") or "greenhouse").casefold() == "greenhouse"
}

state_paths = sorted(output.glob("continuous_greenhouse*_state.json"))
state_paths += sorted(output.glob("greenhouse-tracker-retry.*/state.json"))
by_url = {}
retry_roots = set()

for state_path in state_paths:
    payload = load_json(state_path, {})
    for record in records(payload):
        if not isinstance(record, dict):
            continue
        url = str(record.get("job_url") or record.get("url") or "").strip()
        canonical_url = canonical(url)
        if not canonical_url or canonical_url in confirmed_urls:
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        status = str(record.get("result_status") or result.get("status") or record.get("status") or "UNKNOWN")
        updated = str(record.get("updated_at") or record.get("application_started_at") or record.get("started_at") or "")
        entry = by_url.setdefault(canonical_url, {
            "company": str(record.get("company") or ""),
            "role": str(record.get("title") or record.get("role") or ""),
            "job_url": url,
            "status": status,
            "stage": str(record.get("stage") or ""),
            "submitted": result.get("submitted"),
            "confirmed": result.get("confirmed"),
            "failure_reason": str(result.get("error") or result.get("detail") or record.get("stderr_tail") or "")[-2000:],
            "missing_required": result.get("missing_required", []),
            "updated_at": updated,
            "state_files": set(),
            "artifact_paths": set(),
        })
        entry["state_files"].add(str(state_path.relative_to(repo)))
        if updated >= entry["updated_at"]:
            entry.update({
                "company": str(record.get("company") or entry["company"]),
                "role": str(record.get("title") or record.get("role") or entry["role"]),
                "status": status,
                "stage": str(record.get("stage") or entry["stage"]),
                "submitted": result.get("submitted"),
                "confirmed": result.get("confirmed"),
                "failure_reason": str(result.get("error") or result.get("detail") or record.get("stderr_tail") or "")[-2000:],
                "missing_required": result.get("missing_required", []),
                "updated_at": updated,
            })
        for key in ("document_dir", "result_path"):
            value = str(record.get(key) or "").strip()
            if value:
                entry["artifact_paths"].add(value)
        if state_path.parent.name.startswith("greenhouse-tracker-retry."):
            retry_roots.add(state_path.parent.resolve())

def safe_target(raw):
    path = Path(raw).resolve()
    if path == output or output not in path.parents:
        raise RuntimeError(f"refusing cleanup outside a child of output: {path}")
    return path

deleted_paths = set()
files_deleted = 0
bytes_deleted = 0

def remove_target(path):
    global files_deleted, bytes_deleted
    path = safe_target(path)
    if path in deleted_paths or not path.exists():
        return
    if path.is_dir():
        files = [item for item in path.rglob("*") if item.is_file()]
        files_deleted += len(files)
        bytes_deleted += sum(item.stat().st_size for item in files)
        shutil.rmtree(path)
    elif path.is_file():
        files_deleted += 1
        bytes_deleted += path.stat().st_size
        path.unlink()
    deleted_paths.add(path)

for entry in by_url.values():
    for artifact in sorted(entry["artifact_paths"]):
        remove_target(artifact)
for retry_root in sorted(retry_roots):
    remove_target(retry_root)

rows = []
for entry in sorted(by_url.values(), key=lambda item: (item["company"].casefold(), item["role"].casefold(), item["job_url"])):
    missing = entry["missing_required"]
    if isinstance(missing, list):
        missing = " | ".join(str(item) for item in missing)
    rows.append({
        **{key: value for key, value in entry.items() if key not in {"state_files", "artifact_paths", "missing_required"}},
        "missing_required": str(missing or ""),
        "state_files": " | ".join(sorted(entry["state_files"])),
        "artifact_paths_deleted": " | ".join(sorted(entry["artifact_paths"])),
    })

report = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "confirmed_urls_preserved": len(confirmed_urls),
    "failed_unsubmitted_roles": len(rows),
    "files_deleted": files_deleted,
    "bytes_deleted": bytes_deleted,
    "deleted_paths": [str(path) for path in sorted(deleted_paths)],
    "rows": rows,
}
report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps({key: report[key] for key in ("confirmed_urls_preserved", "failed_unsubmitted_roles", "files_deleted", "bytes_deleted")}, sort_keys=True))
PY
"@
$RemoteCommand = ConvertTo-LfLineEndings $RemoteCommand

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)", $RemoteCommand
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { exit $Execution.ExitCode }
    & $PscpPath -batch -P $Connection.Port -hostkey $Connection.HostKey -pwfile $PasswordFile `
        "$($Connection.User)@$($Connection.Host):$RemoteReport" $OutputJsonPath
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $CleanupCommand = "rm -f '$RemoteReport'"
    & $PlinkPath -ssh -batch -P $Connection.Port -hostkey $Connection.HostKey -pwfile $PasswordFile `
        "$($Connection.User)@$($Connection.Host)" $CleanupCommand | Out-Null
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
