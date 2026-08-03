param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 60
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-inspect"
$RemoteCommand = @"
set -eu
repo=$Repo
systemctl show 'job-app-greenhouse-failed-*.service' --property=Id,ActiveState,SubState,NRestarts,ExecMainStatus
echo '=== ACTIVE WORKER PROCESSES ==='
ps -eo pid,etimes,pcpu,pmem,args --sort=-etimes |
    grep -E 'continuous_source_ats|job_automation\.py' |
    grep -v grep |
    sed -E 's/[A-Za-z0-9.!#$%&*+\/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[REDACTED_EMAIL]/g' || true
echo '=== RECENT WORKER MILESTONES ==='
journalctl \
    -u job-app-greenhouse-failed-core-product-management.service \
    -u job-app-greenhouse-failed-growth-general-marketing.service \
    -u job-app-greenhouse-failed-product-marketing-gtm.service \
    -u job-app-greenhouse-failed-program-project-management.service \
    -u job-app-greenhouse-failed-technical-ai-platform-product-management.service \
    --since '20 minutes ago' --no-pager -n 200 |
    grep -E -A 12 'SOURCE_(PROCESSING|RESULT|RETRY|SLEEP|STOPPED)|CYCLE_(FAILED|COMPLETE)|SUBMITTED|CONFIRMED|REQUIRED_FIELDS|JOB_CONTEXT|DOCUMENT_GENERATION|timed out|timeout|ERROR|Traceback' |
    sed -E 's/[A-Za-z0-9.!#$%&*+\/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[REDACTED_EMAIL]/g' |
    tail -n 100 || true
python3 - "`$repo/output" <<'PY'
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

root = Path(sys.argv[1])
resume_source = root.parent / "data" / "resumes" / "base-resume.txt"
print(json.dumps({
    "artifact": str(resume_source),
    "exists": resume_source.is_file(),
    "bytes": resume_source.stat().st_size if resume_source.is_file() else 0,
}, sort_keys=True))
for path in sorted(root.glob("continuous_greenhouse_failed_*_state.json")):
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = list(payload.get("jobs", {}).values())
    latest = max(records, key=lambda item: str(item.get("updated_at", ""))) if records else {}
    result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
    print(json.dumps({
        "worker": path.stem,
        "records": len(records),
        "company": latest.get("company"),
        "title": latest.get("title"),
        "status": latest.get("status"),
        "result_status": latest.get("result_status"),
        "missing_required": result.get("missing_required", []),
        "detail": result.get("detail") or result.get("error"),
    }, ensure_ascii=False, sort_keys=True))
    diagnostics = []
    for field in ("stdout_tail", "stderr_tail"):
        text = str(latest.get(field, ""))
        text = re.sub(
            r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
            "[REDACTED_EMAIL]",
            text,
        )
        diagnostics.extend(
            line.strip() for line in text.splitlines()
            if re.search(r"required|missing|question|manual.review", line, re.I)
        )
    if diagnostics:
        print(json.dumps({"worker": path.stem, "diagnostics": diagnostics[-12:]}, ensure_ascii=False))

    status_counts = Counter(str(item.get("result_status") or item.get("status") or "UNKNOWN") for item in records)
    missing_fields = Counter()
    affected_jobs = defaultdict(list)
    generation_diagnostics = Counter()
    standard_field_diagnostics = []
    failed_jobs = []
    for item in records:
        status = str(item.get("result_status") or item.get("status") or "UNKNOWN")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        fields = list(result.get("missing_required") or []) + list(result.get("missing_critical") or [])
        combined_tail = "\n".join(str(item.get(field, "")) for field in ("stdout_tail", "stderr_tail"))
        artifact_result = {}
        result_path = Path(str(item.get("result_path") or ""))
        if result_path.is_file():
            try:
                artifact_payload = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(artifact_payload, list) and artifact_payload and isinstance(artifact_payload[0], dict):
                    artifact_result = artifact_payload[0]
                elif isinstance(artifact_payload, dict):
                    artifact_result = artifact_payload
                if isinstance(artifact_result.get("result"), dict):
                    artifact_result = artifact_result["result"]
            except (OSError, json.JSONDecodeError):
                pass
        fields.extend(artifact_result.get("missing_required") or [])
        fields.extend(artifact_result.get("missing_critical") or [])
        for match in re.finditer(r"ENGINE_RESULT_JSON:(\{.*\})", combined_tail):
            try:
                engine_result = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            fields.extend(engine_result.get("missing_required") or [])
            fields.extend(engine_result.get("missing_critical") or [])
        for key in ("missing_required", "missing_critical"):
            for match in re.finditer(rf'"{key}"\s*:\s*(\[[^\]]*\])', combined_tail):
                try:
                    fields.extend(json.loads(match.group(1)))
                except json.JSONDecodeError:
                    continue
        for field in fields:
            normalized = str(field).strip()
            if not normalized:
                continue
            missing_fields[normalized] += 1
            job = f"{item.get('company', '')} | {item.get('title', '')}".strip(" |")
            if job and job not in affected_jobs[normalized]:
                affected_jobs[normalized].append(job)
        standard_names = {
            "first name", "first_name", "last name", "last_name", "email", "resume",
            "legal first name (english)", "what is your legal first name?", "address line 1",
        }
        standard_missing = sorted({str(field).strip() for field in fields if str(field).strip().casefold() in standard_names})
        if standard_missing:
            diagnostic_lines = [
                re.sub(
                    r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
                    "[REDACTED_EMAIL]",
                    line.strip(),
                )
                for line in combined_tail.splitlines()
                if re.search(r"ENGINE_RESULT|missing|required|error|traceback|timeout|navigation|selector|resume", line, re.I)
            ]
            standard_field_diagnostics.append({
                "job": f"{item.get('company', '')} | {item.get('title', '')}".strip(" |"),
                "status": status,
                "fields": standard_missing,
                "diagnostics": diagnostic_lines[-8:],
            })
        if status == "DOCUMENT_GENERATION_FAILED":
            detail = str(result.get("detail") or result.get("error") or item.get("detail") or "").strip()
            if not detail:
                diagnostic_lines = [
                    line.strip() for line in combined_tail.splitlines()
                    if re.search(r"document|generation|resume|cover.letter|pdf|claim|gemini|timeout|error", line, re.I)
                ]
                detail = diagnostic_lines[-1] if diagnostic_lines else "No detailed generation diagnostic recorded"
            generation_diagnostics[detail] += 1
        if status not in {"preparing", "application_started", "SUBMITTED & CONFIRMED"}:
            detail = str(result.get("detail") or result.get("error") or item.get("detail") or "").strip()
            failure_diagnostics = [
                re.sub(
                    r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
                    "[REDACTED_EMAIL]",
                    line.strip(),
                )
                for line in combined_tail.splitlines()
                if re.search(r"unconfigured|required|missing|ENGINE_RESULT|question", line, re.I)
            ]
            if not failure_diagnostics and status == "REQUIRED_FIELDS_NOT_FILLED":
                failure_diagnostics = [
                    re.sub(
                        r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
                        "[REDACTED_EMAIL]",
                        line.strip(),
                    )
                    for line in combined_tail.splitlines() if line.strip()
                ]
            failed_jobs.append({
                "company": item.get("company"),
                "title": item.get("title"),
                "job_url": item.get("job_url"),
                "status": status,
                "missing_fields": sorted(set(fields)),
                "detail": detail or None,
                "diagnostics": failure_diagnostics[-8:],
                "unfilled_controls": sorted({
                    str(label) for section in ("custom_questions", "eeo_fields", "filled_fields")
                    for label, filled in (artifact_result.get(section) or {}).items() if filled is False
                }),
            })
    print(json.dumps({
        "worker": path.stem,
        "status_counts": dict(status_counts),
        "missing_fields": dict(missing_fields),
        "affected_jobs": {field: jobs for field, jobs in affected_jobs.items()},
        "document_generation_diagnostics": dict(generation_diagnostics),
        "standard_field_diagnostics": standard_field_diagnostics,
        "failed_jobs": failed_jobs,
    }, ensure_ascii=False, sort_keys=True))
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
