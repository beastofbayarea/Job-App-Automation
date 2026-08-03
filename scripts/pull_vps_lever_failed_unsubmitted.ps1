param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$OutputPath = "output/vps_lever_failed_unsubmitted.json",
    [ValidateRange(1, 300)] [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PscpPath = Get-RequiredCommandPath -Name "pscp"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$Token = [guid]::NewGuid().ToString("N")
$RemoteReport = "/tmp/lever-failed-unsubmitted-$Token.json"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "lever-failed-pull"
$RemoteCommand = @"
set -eu
repo=$Repo
python3 - "`$repo/output" '$RemoteReport' <<'PY'
import json, sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

root, destination = Path(sys.argv[1]), Path(sys.argv[2])
hosts = {'jobs.lever.co', 'jobs.eu.lever.co'}

def canonical(value):
    try: parsed = urlsplit(str(value or '').strip())
    except ValueError: return None
    if parsed.hostname not in hosts: return None
    parts = [part for part in parsed.path.split('/') if part]
    if parts and parts[-1].lower() == 'apply': parts.pop()
    if len(parts) < 2: return None
    return urlunsplit(('https', parsed.hostname, '/' + '/'.join(parts[:2]), '', ''))

confirmed = set()
ledger = root / 'submission_log.json'
if ledger.is_file():
    payload = json.loads(ledger.read_text(encoding='utf-8'))
    for entry in (payload if isinstance(payload, list) else payload.values()):
        if isinstance(entry, dict) and entry.get('status') == 'SUBMITTED & CONFIRMED':
            url = canonical(entry.get('job_url') or entry.get('url'))
            if url: confirmed.add(url)

paths = [root/'continuous_lever_state.json', root/'vps_application_state.json', root/'vps_application_failures.json']
for name in ('continuous_lever_results', 'vps_application_results'):
    directory = root / name
    if directory.is_dir(): paths.extend(directory.glob('*.json'))
candidates = {}

def add(record, keyed_url=None):
    if not isinstance(record, dict): return
    result = record.get('result') if isinstance(record.get('result'), dict) else {}
    url = canonical(keyed_url or record.get('job_url') or record.get('application_url') or record.get('url') or result.get('job_url') or result.get('url'))
    if not url or url in confirmed: return
    status = str(record.get('result_status') or result.get('status') or record.get('status') or 'SUBMISSION_UNCONFIRMED')
    if status == 'SUBMITTED & CONFIRMED' or record.get('ledger_confirmed') is True: return
    missing = result.get('missing_required') or record.get('missing_required') or []
    if isinstance(missing, list): missing = ' | '.join(str(x).strip() for x in missing if str(x).strip())
    reason = str(result.get('error') or result.get('detail') or record.get('failure_reason') or record.get('stderr_tail') or '').strip()
    if not reason and missing: reason = f'Required fields: {missing}'
    if not reason: reason = 'Submission was not confirmed on the VPS'
    incoming = {
        'company': str(record.get('company') or result.get('company') or '').strip(),
        'role': str(record.get('title') or record.get('role') or result.get('title') or '').strip(),
        'job_url': url, 'status': status, 'failure_reason': reason[-2000:],
        'missing_required': str(missing),
        'updated_at': str(record.get('updated_at') or result.get('updated_at') or ''),
        'source': 'vps',
    }
    old = candidates.get(url)
    if old is None or incoming['updated_at'] >= old['updated_at']: candidates[url] = incoming

def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if canonical(key) and isinstance(child, dict): add(child, key)
            walk(child)
        add(value)
    elif isinstance(value, list):
        for child in value: walk(child)

for path in paths:
    if not path.is_file(): continue
    try: walk(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError): continue
destination.write_text(json.dumps(list(candidates.values()), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
print(f'vps_unconfirmed={len(candidates)} ledger_confirmed_lever={len(confirmed)}')
PY
"@

try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    if ($Execution.ExitCode -ne 0) { throw "VPS Lever report failed: $($Execution.Output -join [Environment]::NewLine)" }
    $Parent = Split-Path -Parent $OutputPath
    if ($Parent) { New-Item -ItemType Directory -Path $Parent -Force | Out-Null }
    $Pull = Invoke-ExternalCommandWithTimeout -FilePath $PscpPath -ArgumentList @(
        "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host):$RemoteReport", $OutputPath
    ) -TimeoutSeconds $TimeoutSeconds
    if ($Pull.ExitCode -ne 0) { throw "VPS Lever report download failed: $($Pull.Output -join [Environment]::NewLine)" }
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    Write-Output "saved=$OutputPath"
} finally {
    & $PlinkPath -ssh -batch -P $Connection.Port -hostkey $Connection.HostKey -pwfile $PasswordFile `
        "$($Connection.User)@$($Connection.Host)" "rm -f '$RemoteReport'" 2>$null | Out-Null
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
