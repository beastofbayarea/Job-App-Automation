param(
    [Parameter(Mandatory)]
    [string[]]$Target,
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot/lib/vps_script_helpers.ps1"
$Connection = Read-VpsConnectionConfig -Path $ConfigPath
$PlinkPath = Get-RequiredCommandPath -Name "plink"
$PasswordFile = New-TemporaryPasswordFile -Password $Connection.Password -Prefix "failed-json-targets"
$Repo = ConvertTo-PosixShellLiteral $RemoteRepoPath.TrimEnd("/")
$TargetPayload = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes(($Target | ConvertTo-Json -Compress))
)
$RemoteCommand = @"
set -eu
repo=$Repo
PYTHONPATH="`$repo/src" "`$repo/.venv/bin/python" - "`$repo/output" '$TargetPayload' <<'PY'
import base64
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_application_automation.core.artifacts import atomic_write_text

output = Path(sys.argv[1])
targets = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
if isinstance(targets, str):
    targets = [targets]
wanted = set()
for target in targets:
    parts = str(target).split("|", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise SystemExit(f"invalid target {target!r}; expected worker|company|title")
    wanted.add(tuple(part.strip().casefold() for part in parts))

state_paths = sorted(output.glob("continuous_greenhouse_failed_*_state.json"))
matched_paths = []
for path in state_paths:
    worker = path.stem.removeprefix("continuous_greenhouse_failed_").removesuffix("_state")
    if any(item[0] == worker.casefold() for item in wanted):
        matched_paths.append(path)
if not matched_paths:
    raise SystemExit("no target worker states found")

units = [f"job-app-greenhouse-failed-{path.stem.removeprefix('continuous_greenhouse_failed_').removesuffix('_state').replace('_', '-')}.service" for path in matched_paths]
subprocess.run(["systemctl", "stop", *units], check=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup = output / "requeue_backups" / f"targeted-{stamp}"
backup.mkdir(parents=True, exist_ok=False)
claims_path = output / "continuous_greenhouse_failed_claims.json"
for path in [claims_path, *matched_paths]:
    if path.is_file():
        shutil.copy2(path, backup / path.name)

claims = json.loads(claims_path.read_text(encoding="utf-8"))
matched = []
for path in matched_paths:
    state = json.loads(path.read_text(encoding="utf-8"))
    worker = path.stem.removeprefix("continuous_greenhouse_failed_").removesuffix("_state")
    for key, record in list(state.get("jobs", {}).items()):
        if not isinstance(record, dict):
            continue
        identity = (worker.casefold(), str(record.get("company", "")).strip().casefold(), str(record.get("title", "")).strip().casefold())
        if identity not in wanted:
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        status = str(record.get("result_status") or result.get("status") or "")
        if status != "REQUIRED_FIELDS_NOT_FILLED":
            raise SystemExit(f"refusing target with status {status}: {identity}")
        job_url = str(record.get("job_url") or key)
        del state["jobs"][key]
        for claim in claims.get("jobs", {}).values():
            if isinstance(claim, dict) and claim.get("job_url") == job_url:
                claim["status"] = "retry_requested"
        matched.append("|".join(identity))
    atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")

missing = sorted("|".join(item) for item in wanted - {tuple(item.split("|", 2)) for item in matched})
if missing:
    raise SystemExit(f"targets not found: {missing}")
claims["updated_at"] = datetime.now(timezone.utc).isoformat()
atomic_write_text(claims_path, json.dumps(claims, indent=2, sort_keys=True) + "\n")
subprocess.run(["systemctl", "reset-failed", *units], check=False)
subprocess.run(["systemctl", "start", *units], check=True)
print(f"backup={backup}")
for item in sorted(matched):
    print(f"requeued={item}")
subprocess.run(["systemctl", "show", *units, "--property=Id,ActiveState,SubState,NRestarts"], check=True)
PY
"@
try {
    $Execution = Invoke-ExternalCommandWithTimeout -FilePath $PlinkPath -ArgumentList @(
        "-ssh", "-batch", "-P", $Connection.Port, "-hostkey", $Connection.HostKey,
        "-pwfile", $PasswordFile, "$($Connection.User)@$($Connection.Host)",
        (ConvertTo-LfLineEndings $RemoteCommand)
    ) -TimeoutSeconds $TimeoutSeconds
    $Execution.Output | ForEach-Object { Write-Output ([string]$_) }
    if ($Execution.ExitCode -ne 0) { throw "Targeted failed JSON requeue failed with exit code $($Execution.ExitCode)" }
} finally {
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
