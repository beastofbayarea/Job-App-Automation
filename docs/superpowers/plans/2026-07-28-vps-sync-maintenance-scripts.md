# VPS Sync Maintenance Scripts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four maintenance scripts around the existing VPS search-sync pipeline: an on-demand search trigger, a sync-freshness check, output pruning, and VPS log rotation.

**Architecture:** Four standalone PowerShell/config scripts in `scripts/`, each a self-contained CLI tool with no shared module (matching the existing `pull_search_output.ps1` pattern — no test framework exists for `.ps1`/`.sh` files in this repo, so verification is manual script execution, not pytest).

**Tech Stack:** PowerShell 7 (pwsh), `plink.exe` (PuTTY, already installed) for password SSH, bash + logrotate on the VPS side.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-28-vps-sync-maintenance-scripts-design.md`
- Staleness threshold: 24 hours (from spec).
- Prune age cutoff: 14 days, dry-run by default (from spec).
- `trigger_vps_search.ps1` must not pull after a remote failure (from spec).
- Logrotate config is written to the repo but **not** installed on the VPS by this plan — that's a manual one-time step for the user (from spec).
- `config/vps_config.json` schema (already exists, do not modify): `{"vps": {"host": "...", "ssh_user": "...", "ssh_password": {"value": "...", "secret": true}}}`.
- Coverage report schema (from `src/job_application_automation/search/job_boards.py:2996`): JSON object with a `generated_at` key holding an ISO 8601 UTC timestamp string.

---

### Task 1: `scripts/check_sync_freshness.ps1`

**Files:**
- Create: `scripts/check_sync_freshness.ps1`

**Interfaces:**
- Consumes: `output/job_search_coverage.json` (JSON, key `generated_at` = ISO 8601 UTC string, e.g. `"2026-07-28T03:00:12+00:00"`).
- Produces: stdout message, exit code 0 (fresh) or 1 (stale or missing/unparseable file). No other task depends on this script's internals.
- Params: `-Path <string>` (default `output/job_search_coverage.json`), `-ThresholdHours <int>` (default 24).

- [ ] **Step 1: Write the script**

```powershell
# scripts/check_sync_freshness.ps1
# Reports whether the local coverage report reflects a recent VPS sync.
param(
    [string]$Path = "output/job_search_coverage.json",
    [int]$ThresholdHours = 24
)

if (-not (Test-Path $Path)) {
    Write-Warning "STALE: $Path not found. Run scripts\pull_search_output.ps1 first."
    exit 1
}

try {
    $Report = Get-Content $Path -Raw | ConvertFrom-Json
} catch {
    Write-Error "STALE: $Path is not valid JSON."
    exit 1
}

if (-not $Report.generated_at) {
    Write-Error "STALE: $Path has no 'generated_at' field."
    exit 1
}

$Generated = [DateTimeOffset]::Parse($Report.generated_at)
$AgeHours = [Math]::Round(([DateTimeOffset]::UtcNow - $Generated).TotalHours, 1)

if ($AgeHours -gt $ThresholdHours) {
    Write-Warning "STALE (age: ${AgeHours}h, threshold: ${ThresholdHours}h)"
    exit 1
} else {
    Write-Host "OK (age: ${AgeHours}h)"
    exit 0
}
```

- [ ] **Step 2: Manually verify against the current file**

Run: `pwsh scripts\check_sync_freshness.ps1`
Expected: Since `output/job_search_coverage.json` currently contains `{}` (no `generated_at` key), output is `STALE: output/job_search_coverage.json has no 'generated_at' field.` and exit code 1. Confirm with `echo $LASTEXITCODE` → `1`.

- [ ] **Step 3: Verify against a synthetic fresh file**

Run:
```powershell
'{"generated_at": "' + (Get-Date).ToUniversalTime().ToString("o") + '"}' | Set-Content .test_coverage.json
pwsh scripts\check_sync_freshness.ps1 -Path .test_coverage.json
```
Expected: `OK (age: 0h)`, exit code 0.

- [ ] **Step 4: Verify against a synthetic stale file**

Run:
```powershell
'{"generated_at": "' + (Get-Date).ToUniversalTime().AddHours(-48).ToString("o") + '"}' | Set-Content .test_coverage.json
pwsh scripts\check_sync_freshness.ps1 -Path .test_coverage.json
Remove-Item .test_coverage.json
```
Expected: `STALE (age: 48h, threshold: 24h)`, exit code 1. Clean up the synthetic file afterward.

- [ ] **Step 5: Commit**

```bash
git add scripts/check_sync_freshness.ps1
git commit -m "feat(scripts): add sync freshness check"
```

---

### Task 2: `scripts/prune_old_outputs.ps1`

**Files:**
- Create: `scripts/prune_old_outputs.ps1`

**Interfaces:**
- Consumes: files matching `output/*_Resume.pdf` and `output/*_Cover_Letter.pdf` (naming from `src/job_application_automation/resume/generate.py:912` and `cover_letter.py:264`).
- Produces: stdout report of matched files; deletes only when `-Delete` is passed. No other task depends on this script's internals.
- Params: `-Days <int>` (default 14), `-Delete` (switch, default off = dry run), `-OutputDir <string>` (default `output`).

- [ ] **Step 1: Write the script**

```powershell
# scripts/prune_old_outputs.ps1
# Lists (or deletes, with -Delete) generated resume/cover-letter PDFs older than -Days.
param(
    [int]$Days = 14,
    [switch]$Delete,
    [string]$OutputDir = "output"
)

$Cutoff = (Get-Date).AddDays(-$Days)
$Patterns = @("*_Resume.pdf", "*_Cover_Letter.pdf")

$Candidates = foreach ($Pattern in $Patterns) {
    Get-ChildItem -Path $OutputDir -Filter $Pattern -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $Cutoff }
}

if (-not $Candidates) {
    Write-Host "No files older than $Days days found in $OutputDir."
    exit 0
}

$TotalBytes = ($Candidates | Measure-Object -Property Length -Sum).Sum
$TotalMB = [Math]::Round($TotalBytes / 1MB, 2)

if ($Delete) {
    $Candidates | Remove-Item -Force
    Write-Host "Deleted $($Candidates.Count) file(s), freed ${TotalMB}MB."
} else {
    Write-Host "DRY RUN: $($Candidates.Count) file(s) older than $Days days (${TotalMB}MB total). Re-run with -Delete to remove."
    $Candidates | ForEach-Object { Write-Host "  $($_.Name) ($($_.LastWriteTime))" }
}
```

- [ ] **Step 2: Manually verify dry-run against real output/**

Run: `pwsh scripts\prune_old_outputs.ps1`
Expected: since the current `output/` PDFs are all from 2026-07-27 (1 day old, per the earlier `ls -la output/` in this session) and default cutoff is 14 days, output is `No files older than 14 days found in output.` — confirms it correctly does *not* flag recent files.

- [ ] **Step 3: Verify it detects old files without deleting**

Run: `pwsh scripts\prune_old_outputs.ps1 -Days 0`
Expected: lists all `*_Resume.pdf`/`*_Cover_Letter.pdf` files in `output/` (since every file is "older" than 0 days from now) with a total size, and does **not** delete anything (no `-Delete` passed). Confirm file count in `output/` is unchanged afterward via `ls output | measure`.

- [ ] **Step 4: Verify -Delete actually removes on a throwaway copy**

Run in a scratch dir (not the real `output/`), to avoid touching real data:
```powershell
New-Item -ItemType Directory -Force .test_prune | Out-Null
Copy-Item output\*.pdf .test_prune -ErrorAction SilentlyContinue
if (-not (Get-ChildItem .test_prune -File)) { "sample_Resume.pdf" | Set-Content .test_prune\sample_Resume.pdf }
$OldFile = Get-ChildItem .test_prune -File | Select-Object -First 1
Set-ItemProperty $OldFile.FullName -Name LastWriteTime -Value (Get-Date).AddDays(-30)
pwsh scripts\prune_old_outputs.ps1 -OutputDir .test_prune -Days 14 -Delete
Get-ChildItem .test_prune -File
Remove-Item -Recurse -Force .test_prune
```
Expected: the artificially-aged file is deleted, reported in the "Deleted N file(s)" message; clean up `.test_prune` afterward.

- [ ] **Step 5: Commit**

```bash
git add scripts/prune_old_outputs.ps1
git commit -m "feat(scripts): add output pruning script for old resumes/cover letters"
```

---

### Task 3: VPS log rotation config

**Files:**
- Create: `scripts/vps-sync.logrotate`
- Modify: `scripts/vps_search_sync.sh` (header comment block, after the existing "One-time setup on the VPS" list at lines 10-16)

**Interfaces:**
- Produces: a logrotate config file installed manually by the user on the VPS. No other task depends on this.

- [ ] **Step 1: Write the logrotate config**

```
# scripts/vps-sync.logrotate
# Install on the VPS with: sudo cp scripts/vps-sync.logrotate /etc/logrotate.d/vps-sync
/root/Job-App-Automation/output/vps_sync.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
```

- [ ] **Step 2: Add setup step to `vps_search_sync.sh` header comment**

In `scripts/vps_search_sync.sh`, the existing header (lines 10-16) reads:

```
# One-time setup on the VPS before this script is scheduled:
#   1. git clone the repo, create a Python venv, `pip install -r requirements.txt`,
#      `playwright install --with-deps chromium`.
#   2. Generate a deploy key and add it to the GitHub repo (Settings > Deploy
#      keys) with "Allow write access" checked, scoped to this repo only:
#        ssh-keygen -t ed25519 -C "vps-search-sync" -f ~/.ssh/vps_search_sync -N ""
#   3. Add a cron entry, e.g.: 0 3 * * * REPO_DIR/scripts/vps_search_sync.sh >> REPO_DIR/output/vps_sync.log 2>&1
```

Add a step 4 immediately after step 3, before the blank line that precedes `set -euo pipefail`:

```
#   4. Install log rotation for the cron output so vps_sync.log doesn't grow
#      unbounded:
#        sudo cp scripts/vps-sync.logrotate /etc/logrotate.d/vps-sync
```

- [ ] **Step 3: Manually verify the logrotate config syntax locally**

Logrotate isn't installed on Windows, so syntax can't be checked with `logrotate -d` locally. Instead, visually confirm the file has no unbalanced braces and the path matches the cron entry's log path (`REPO_DIR/output/vps_sync.log`) by comparing against step 3 of the existing header comment. Note in the commit message that live `logrotate -d` verification is deferred to the user running it on the VPS after manual install (per the spec, this plan does not SSH in to install it).

- [ ] **Step 4: Commit**

```bash
git add scripts/vps-sync.logrotate scripts/vps_search_sync.sh
git commit -m "feat(scripts): add logrotate config for vps_sync.log"
```

---

### Task 4: `scripts/trigger_vps_search.ps1`

**Files:**
- Create: `scripts/trigger_vps_search.ps1`

**Interfaces:**
- Consumes: `config/vps_config.json` (`vps.host`, `vps.ssh_user`, `vps.ssh_password.value`); `plink.exe` on PATH; `scripts\pull_search_output.ps1` (Task-independent, already exists — no params needed, invoked with no arguments so it uses its own `-Branch` default).
- Produces: nothing consumed by other tasks.
- Params: `-RemoteRepoPath <string>` (required, no default — the remote clone path isn't recorded anywhere in the repo), `-ConfigPath <string>` (default `config/vps_config.json`).

- [ ] **Step 1: Write the script**

```powershell
# scripts/trigger_vps_search.ps1
# Runs an out-of-cycle VPS search instead of waiting for the daily cron, then
# pulls the fresh output locally on success.
param(
    [Parameter(Mandatory = $true)]
    [string]$RemoteRepoPath,
    [string]$ConfigPath = "config/vps_config.json"
)

if (-not (Test-Path $ConfigPath)) {
    Write-Error "VPS config not found at $ConfigPath"
    exit 1
}

$PlinkCmd = Get-Command plink -ErrorAction SilentlyContinue
if (-not $PlinkCmd) {
    Write-Error "plink.exe not found on PATH. Install PuTTY or add it to PATH."
    exit 1
}

$Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$VpsHost = $Config.vps.host
$SshUser = $Config.vps.ssh_user
$SshPassword = $Config.vps.ssh_password.value

if (-not $VpsHost -or -not $SshUser -or -not $SshPassword) {
    Write-Error "$ConfigPath is missing vps.host, vps.ssh_user, or vps.ssh_password.value"
    exit 1
}

$RemoteCommand = "bash $RemoteRepoPath/scripts/vps_search_sync.sh"
Write-Host "Running remote search on $VpsHost..."

& plink -ssh -batch -pw $SshPassword "$SshUser@$VpsHost" $RemoteCommand
$RemoteExitCode = $LASTEXITCODE

if ($RemoteExitCode -ne 0) {
    Write-Error "Remote search failed (exit code $RemoteExitCode). Not pulling output."
    exit $RemoteExitCode
}

Write-Host "Remote search finished. Pulling output locally..."
& pwsh "$PSScriptRoot\pull_search_output.ps1"
exit $LASTEXITCODE
```

- [ ] **Step 2: Manually verify parameter validation without touching the real VPS**

Run: `pwsh scripts\trigger_vps_search.ps1`
Expected: PowerShell's mandatory-parameter prompt/error for missing `-RemoteRepoPath` (since it's `Mandatory = $true`), confirming the script fails closed rather than running with an empty remote path.

- [ ] **Step 3: Manually verify config-missing handling**

Run: `pwsh scripts\trigger_vps_search.ps1 -RemoteRepoPath /root/Job-App-Automation -ConfigPath .does-not-exist.json`
Expected: `VPS config not found at .does-not-exist.json`, exit code 1, no plink invocation attempted.

- [ ] **Step 4: Confirm plink connectivity to the real VPS (requires user's go-ahead — this reaches a live remote host)**

Before running end-to-end, confirm with the user that it's OK to SSH into the real VPS for this test (host `2.24.28.180` / `srv1576573.hstgr.cloud` from `config/vps_config.json`). If approved, run:
```powershell
pwsh scripts\trigger_vps_search.ps1 -RemoteRepoPath /root/Job-App-Automation
```
Expected: plink connects, streams the remote `vps_search_sync.sh` output, and on success runs `pull_search_output.ps1` automatically. If the remote repo path is wrong, the SSH command itself will report `bash: /path: No such file or directory` — confirm the correct path with the user first via `plink -ssh -batch -pw <password> root@<host> "pwd; ls"` if unknown, rather than guessing.

- [ ] **Step 5: Commit**

```bash
git add scripts/trigger_vps_search.ps1
git commit -m "feat(scripts): add on-demand VPS search trigger"
```

---

## Self-Review Notes

- **Spec coverage:** All 4 spec items map 1:1 to Tasks 1-4. Out-of-scope item (VPS password rotation) is intentionally not a task.
- **Placeholder scan:** No TBD/TODO; all steps have literal code or literal commands.
- **Type/interface consistency:** `check_sync_freshness.ps1` and `prune_old_outputs.ps1` are fully independent (no shared interface). `trigger_vps_search.ps1` calls `pull_search_output.ps1` with zero arguments, matching that script's existing `param()` block (all defaults, `-Branch` defaults to `"vps-search-output"`) — verified against the file's current content earlier in this session.
- **Risk flag carried into Task 4:** step 4 requires live-VPS confirmation before execution since it's the one step in this plan that reaches a shared/remote system, consistent with the "check before hard-to-reverse or remote actions" rule.
