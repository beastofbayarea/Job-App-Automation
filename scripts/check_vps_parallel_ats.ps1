# Reads every continuous ATS service and the search service without changing work.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$LogLines = 60,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
)

. "$PSScriptRoot\vps_script_helpers.ps1"

try {
    $Connection = Read-VpsConnectionConfig -Path $ConfigPath
    $PlinkPath = Get-RequiredCommandPath -Name "plink"
} catch {
    Write-Error $_
    exit 1
}

$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$RemoteCommand = @"
set -eu
repo=$Repo
printf '%s\n' '=== VPS CAPACITY ==='
date --iso-8601=seconds
uptime
free -h
printf '%s\n' '=== CONTINUOUS SEARCH SERVICE ==='
systemctl show job-app-search-sync.service \
  --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp \
  2>/dev/null || true
printf '%s\n' '=== PARALLEL ATS SERVICES ==='
ats_services=`$(systemctl list-unit-files 'job-app-*.service' --no-legend --no-pager |
  awk '{print `$1}' |
  while read -r service; do
    exec_start=`$(systemctl show "`$service" --property=ExecStart --value 2>/dev/null || true)
    case "`$exec_start" in
      *continuous_ats*|*continuous_source_ats*|*continuous-ashby*|*continuous-greenhouse*|*continuous-lever*)
        printf '%s\n' "`$service"
        ;;
    esac
  done)
if [ -n "`$ats_services" ]; then
  systemctl show `$ats_services \
    --property=Id,LoadState,UnitFileState,ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp \
    2>/dev/null || true
else
  printf '%s\n' 'No continuous ATS services installed.'
fi
printf '%s\n' '=== SEARCH AND ATS PROCESSES ==='
ps -eo pid,ppid,lstart,etime,%cpu,%mem,rss,stat,args |
  grep -E '[v]ps_continuous_search_sync|[v]ps_search_sync.sh|[c]ontinuous-(ashby|greenhouse|lever)|[c]ontinuous_(source_)?ats|[j]ob_automation.py (apply|search)' |
  grep -v '[b]ash -c set -eu repo=' |
  sed -E 's/(--email )[[:graph:]]+/\1[REDACTED]/g' || true
printf '%s\n' '=== PROVIDER STATE ==='
python3 - "`$repo/config/candidate_email_pool.json" "`$repo"/output/continuous_*_state.json <<'PY'
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

now = datetime.now(timezone.utc)
pool_path = Path(sys.argv[1])
pool = {
    str(email).strip().casefold()
    for email in json.loads(pool_path.read_text(encoding="utf-8"))
    if str(email).strip()
}
for raw_path in sys.argv[2:]:
    path = Path(raw_path)
    provider = path.name.removeprefix("continuous_").removesuffix("_state.json")
    if not path.is_file():
        print(f"{provider}: MISSING")
        continue
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = [value for value in payload.get("jobs", {}).values() if isinstance(value, dict)]
    counts = Counter(str(record.get("status", "unknown")) for record in records)
    print(f"{provider}: status_counts=" + json.dumps(counts, sort_keys=True))
    result_counts = Counter(str(record.get("result_status", "MISSING")) for record in records)
    print(f"{provider}: result_counts=" + json.dumps(result_counts, sort_keys=True))
    selected_emails = [
        str(record.get("email", "")).strip().casefold()
        for record in records
        if str(record.get("email", "")).strip()
    ]
    email_counts = Counter(selected_emails)
    print(
        f"{provider}: email_selection="
        + json.dumps(
            {
                "records_with_email": len(selected_emails),
                "unique_selected": len(email_counts),
                "pool_size": len(pool),
                "outside_pool": sum(
                    count for email, count in email_counts.items() if email not in pool
                ),
                "max_reuse": max(email_counts.values(), default=0),
            },
            sort_keys=True,
        )
    )
    invalid_confirmed = [
        record
        for record in records
        if record.get("status") == "confirmed"
        and (
            record.get("result_status") != "SUBMITTED & CONFIRMED"
            or record.get("ledger_confirmed") is not True
        )
    ]
    print(f"{provider}: invalid_confirmed={len(invalid_confirmed)}")
    timestamps = sorted(
        str(record.get("updated_at", ""))
        for record in records
        if record.get("updated_at")
    )
    if timestamps:
        try:
            latest_dt = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            stale_seconds = max(0, int((now - latest_dt).total_seconds()))
        except ValueError:
            stale_seconds = "invalid_timestamp"
        print(
            f"{provider}: first_updated={timestamps[0]} "
            f"latest_updated={timestamps[-1]} stale_seconds={stale_seconds}"
        )
    manual_records = [record for record in records if record.get("status") == "manual_review"]
    manual_diagnostics = "\n".join(
        str(record.get(key, ""))
        for record in manual_records
        for key in ("result_status", "stderr_tail", "stdout_tail")
    )
    keyword_counts = {
        "captcha": len(re.findall("captcha", manual_diagnostics, flags=re.IGNORECASE)),
        "unconfirmed": len(
            re.findall("unconfirmed", manual_diagnostics, flags=re.IGNORECASE)
        ),
        "timeout": len(
            re.findall(r"timed? ?out|timeout", manual_diagnostics, flags=re.IGNORECASE)
        ),
    }
    print(
        f"{provider}: manual_review={len(manual_records)} "
        "diagnostic_keywords=" + json.dumps(keyword_counts, sort_keys=True)
    )
    missing_fields = Counter()
    result_errors = Counter()
    captcha_results = 0
    for record in records:
        result = record.get("result")
        if not isinstance(result, dict):
            continue
        missing = result.get("missing_required", [])
        if isinstance(missing, list):
            missing_fields.update(str(field).strip()[:160] for field in missing if field)
        error = str(result.get("error") or result.get("detail") or "").strip()
        if error:
            error = re.sub(
                r"(?i)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}",
                "[REDACTED_EMAIL]",
                error,
            )
            result_errors[error[:160]] += 1
        if result.get("captcha_present") is True:
            captcha_results += 1
    print(
        f"{provider}: captcha_results={captcha_results} "
        "top_missing_required="
        + json.dumps(missing_fields.most_common(8), ensure_ascii=False)
    )
    print(
        f"{provider}: top_result_errors="
        + json.dumps(result_errors.most_common(5), ensure_ascii=False)
    )
    if records:
        for record in sorted(
            records,
            key=lambda candidate: str(candidate.get("updated_at", "")),
            reverse=True,
        )[:5]:
            summary = {
                key: record.get(key)
                for key in (
                    "status",
                    "stage",
                    "company",
                    "title",
                    "result_status",
                    "ledger_confirmed",
                    "exit_code",
                    "timed_out",
                    "updated_at",
                )
                if key in record
            }
            result = record.get("result")
            if isinstance(result, dict):
                missing = result.get("missing_required")
                if isinstance(missing, list) and missing:
                    summary["missing_required"] = [
                        str(field).strip()[:160] for field in missing[:8]
                    ]
                if "captcha_present" in result:
                    summary["captcha_present"] = result.get("captcha_present")
            print(f"{provider}: recent=" + json.dumps(summary, sort_keys=True))
PY
printf '%s\n' '=== CONFIRMED SUBMISSION LEDGER ==='
python3 - "`$repo/output/submission_log.json" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

path = Path(sys.argv[1])
if not path.is_file():
    print("MISSING")
    raise SystemExit
payload = json.loads(path.read_text(encoding="utf-8"))
records = list(payload.values()) if isinstance(payload, dict) else list(payload)
ats_counts = Counter()
status_counts = Counter()
day_counts = Counter()
canonical_urls = []
for record in records:
    if not isinstance(record, dict):
        continue
    url = str(record.get("job_url", ""))
    host = urlsplit(url).netloc.lower()
    provider = str(record.get("ats", "")).strip().lower()
    if not provider:
        provider = next(
            (
                candidate
                for candidate in (
                    "ashby",
                    "greenhouse",
                    "lever",
                    "smartrecruiters",
                    "workable",
                )
                if candidate in host
            ),
            "unknown",
        )
    ats_counts[provider] += 1
    status_counts[str(record.get("status", "MISSING"))] += 1
    day_counts[str(record.get("applied_at", ""))[:10]] += 1
    parsed = urlsplit(url)
    canonical_urls.append(
        urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                "",
                "",
            )
        )
    )
duplicate_count = sum(
    count - 1 for count in Counter(canonical_urls).values() if count > 1
)
print(f"entries={len(records)}")
print("ats_counts=" + json.dumps(ats_counts, sort_keys=True))
print("status_counts=" + json.dumps(status_counts, sort_keys=True))
print("applied_days=" + json.dumps(day_counts, sort_keys=True))
print(f"duplicate_canonical_urls={duplicate_count}")
PY
printf '%s\n' '=== PROVIDER RESULT ARTIFACTS ==='
providers=`$(
  for service in `$ats_services; do
    basename "`$service" .service | sed -e 's/^job-app-//' -e 's/-/_/g'
  done
  for state_file in "`$repo"/output/continuous_*_state.json; do
    [ -e "`$state_file" ] || continue
    basename "`$state_file" | sed -e 's/^continuous_//' -e 's/_state\.json`$//'
  done
)
providers=`$(printf '%s\n' "`$providers" | sed '/^`$/d' | sort -u)
for provider in `$providers; do
  result_dir="`$repo/output/continuous_`${provider}`_results"
  result_count=`$(find "`$result_dir" -maxdepth 1 -type f -name '*.json' 2>/dev/null |
    wc -l)
  latest_result=`$(find "`$result_dir" -maxdepth 1 -type f -name '*.json' \
    -printf '%T@|%p\n' 2>/dev/null | sort -nr | head -n 1 || true)
  printf '%s: result_files=%s latest=%s\n' \
    "`$provider" "`$result_count" "`$latest_result"
done
printf '%s\n' '=== JOURNAL EVENT COUNTS ==='
python3 - `$ats_services <<'PY'
import json
import re
import subprocess
import sys
from collections import Counter

for unit in sys.argv[1:]:
    provider = unit.removeprefix("job-app-").removesuffix(".service").replace("-", "_")
    journal = subprocess.run(
        [
            "journalctl",
            "-u",
            unit,
            "-b",
            "--no-pager",
            "-o",
            "cat",
        ],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    counts = {
        "cycle_start": len(re.findall(r"_CYCLE_START", journal)),
        "confirmed": len(re.findall(r"_CYCLE_CONFIRMED", journal)),
        "failed": len(re.findall(r"_CYCLE_FAILED", journal)),
        "refresh_ok": len(
            re.findall(
                r"_REFRESH_FINISHED exit_code=0 timed_out=False",
                journal,
            )
        ),
        "refresh_timeout": len(
            re.findall(
                r"_REFRESH_FINISHED exit_code=124 timed_out=True",
                journal,
            )
        ),
        "captcha_circuit_open": len(
            re.findall(r"_CAPTCHA_CIRCUIT_OPEN", journal)
        ),
        "possible_spam_circuit_open": len(
            re.findall(r"_POSSIBLE_SPAM_CIRCUIT_OPEN", journal)
        ),
        "application_rate_limit_open": len(
            re.findall(r"_APPLICATION_RATE_LIMIT_OPEN", journal)
        ),
        "traceback": len(re.findall(r"Traceback", journal)),
        "exception": len(re.findall(r"_CYCLE_EXCEPTION", journal)),
        "oom_or_killed": len(
            re.findall(
                r"out of memory|oom-kill|killed process",
                journal,
                flags=re.IGNORECASE,
            )
        ),
    }
    failure_statuses = Counter(
        re.findall(r"_CYCLE_FAILED[^\n]* status=([^ ]+)", journal)
    )
    print(
        f"{provider}: events="
        + json.dumps(counts, sort_keys=True)
        + " failure_statuses="
        + json.dumps(failure_statuses, sort_keys=True)
    )
PY
printf '%s\n' '=== CONTINUOUS SEARCH RUN STATUS ==='
if [ -f "`$repo/output/vps_run_status.json" ]; then
  cat "`$repo/output/vps_run_status.json"
else
  printf '%s\n' 'MISSING'
fi
search_journal=`$(journalctl -u job-app-search-sync.service -b --no-pager -o cat \
  2>/dev/null || true)
printf 'search_cycles_started=%s\n' \
  "`$(printf '%s\n' "`$search_journal" | grep -c 'Beginning VPS search sync cycle' || true)"
printf 'search_cycles_completed=%s\n' \
  "`$(printf '%s\n' "`$search_journal" | grep -c 'search sync cycle completed successfully' || true)"
printf 'search_cycles_failed=%s\n' \
  "`$(printf '%s\n' "`$search_journal" | grep -c 'search sync cycle finished with exit status' || true)"
printf '%s\n' '=== CONTINUOUS SEARCH JOURNAL ==='
printf '%s\n' "`$search_journal" | tail -n $LogLines
for service in `$ats_services; do
  printf '=== %s JOURNAL ===\n' "`$service"
  journalctl -u "`$service" -n $LogLines --no-pager 2>/dev/null || true
done
"@

$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "vps-parallel-ats"
try {
    $Execution = Invoke-ExternalCommandWithTimeout `
        -FilePath $PlinkPath `
        -ArgumentList @(
            "-ssh",
            "-batch",
            "-P",
            $Connection.Port,
            "-hostkey",
            $Connection.HostKey,
            "-pwfile",
            $PasswordFile,
            "$($Connection.User)@$($Connection.Host)",
            $RemoteCommand
        ) `
        -TimeoutSeconds $TimeoutSeconds
    foreach ($OutputLine in $Execution.Output) {
        Write-Output ([string]$OutputLine)
    }
    if ($Execution.TimedOut) {
        Write-Error "VPS parallel ATS status check timed out after $TimeoutSeconds seconds."
    }
    exit $Execution.ExitCode
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
