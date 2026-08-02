param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$OutputPath = "output/vps_ashby_failed_unsubmitted.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PscpPath = Get-RequiredCommandPath -Name "pscp"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$Token = [guid]::NewGuid().ToString("N")
$RemoteReport = "/tmp/ashby-failed-unsubmitted-$Token.json"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "ashby-failed-pull"
$RemoteCommand = @"
set -eu
repo=$Repo
python3 - "`$repo/output" '$RemoteReport' <<'PY'
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

root = Path(sys.argv[1])
destination = Path(sys.argv[2])

def canonical(value):
    try:
        parsed = urlsplit(str(value or '').strip())
    except ValueError:
        return None
    if parsed.hostname not in {'jobs.ashbyhq.com', 'ashbyhq.com'}:
        return None
    path = parsed.path.rstrip('/')
    if len([part for part in path.split('/') if part]) < 2:
        return None
    return urlunsplit(('https', 'jobs.ashbyhq.com', path, '', ''))

confirmed = set()
ledger = root / 'submission_log.json'
if ledger.is_file():
    payload = json.loads(ledger.read_text(encoding='utf-8'))
    entries = payload if isinstance(payload, list) else list(payload.values())
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get('status') != 'SUBMITTED & CONFIRMED':
            continue
        url = canonical(entry.get('job_url') or entry.get('url'))
        if url:
            confirmed.add(url)

paths = [
    root / 'continuous_ashby_state.json',
    root / 'vps_application_state.json',
    root / 'vps_application_failures.json',
]
for directory_name in ('continuous_ashby_results', 'vps_application_results'):
    directory = root / directory_name
    if directory.is_dir():
        paths.extend(directory.glob('*.json'))

candidates = {}

def add(record, keyed_url=None):
    if not isinstance(record, dict):
        return
    result = record.get('result') if isinstance(record.get('result'), dict) else {}
    url = canonical(
        keyed_url or record.get('job_url') or record.get('application_url')
        or record.get('url') or result.get('job_url') or result.get('url')
    )
    if not url or url in confirmed:
        return
    status = str(
        record.get('result_status') or result.get('status') or record.get('status')
        or 'SUBMISSION_UNCONFIRMED'
    )
    if status == 'SUBMITTED & CONFIRMED' or record.get('ledger_confirmed') is True:
        return
    missing = result.get('missing_required') or record.get('missing_required') or []
    if isinstance(missing, list):
        missing = ' | '.join(str(item).strip() for item in missing if str(item).strip())
    reason = str(
        result.get('error') or result.get('detail') or record.get('failure_reason')
        or record.get('stderr_tail') or ''
    ).strip()
    if not reason and missing:
        reason = f'Required fields: {missing}'
    if not reason:
        reason = 'Submission was not confirmed on the VPS'
    incoming = {
        'company': str(record.get('company') or result.get('company') or '').strip(),
        'role': str(record.get('title') or record.get('role') or result.get('title') or '').strip(),
        'job_url': url,
        'status': status,
        'failure_reason': reason[-2000:],
        'missing_required': str(missing),
        'updated_at': str(record.get('updated_at') or result.get('updated_at') or ''),
        'source': 'vps',
    }
    existing = candidates.get(url)
    if existing is None or incoming['updated_at'] >= existing['updated_at']:
        candidates[url] = incoming

def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if canonical(key) and isinstance(child, dict):
                add(child, key)
            walk(child)
        add(value)
    elif isinstance(value, list):
        for child in value:
            walk(child)

for path in paths:
    if not path.is_file():
        continue
    try:
        walk(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError):
        continue

destination.write_text(
    json.dumps(list(candidates.values()), indent=2, ensure_ascii=False) + '\n',
    encoding='utf-8',
)
print(f'vps_unconfirmed={len(candidates)} ledger_confirmed_ashby={len(confirmed)}')
PY
"@

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    if ($Execution.ExitCode -ne 0) {
        throw "VPS Ashby report generation failed: $($Execution.Output -join [Environment]::NewLine)"
    }
    $OutputDirectory = Split-Path -Parent $OutputPath
    if ($OutputDirectory) {
        New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    }
    $Pull = Invoke-ExternalCommandWithTimeout -FilePath $PscpPath -ArgumentList @(
        "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile,
        "$($Connection.User)@$($Connection.Host):$RemoteReport", $OutputPath
    ) -TimeoutSeconds $TimeoutSeconds
    if ($Pull.ExitCode -ne 0) {
        throw "VPS Ashby report download failed: $($Pull.Output -join [Environment]::NewLine)"
    }
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    Write-Output "saved=$OutputPath"
} finally {
    $CleanupCommand = "rm -f '$RemoteReport'"
    & $PlinkPath -ssh -batch -P $Connection.Port -hostkey $Connection.HostKey `
        -pwfile $PasswordFile "$($Connection.User)@$($Connection.Host)" $CleanupCommand `
        2>$null | Out-Null
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
