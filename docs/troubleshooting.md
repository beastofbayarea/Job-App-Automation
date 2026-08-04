# Troubleshooting

This guide organizes common issues by symptom with diagnostic commands, log interpretation, and resolution steps. For quick diagnostic commands, see [Quick Reference](quick-reference.md). For safe operating procedures, see [Operations Runbook](operations-runbook.md).

## Quick Diagnostic Commands


This guide organizes common issues by symptom with diagnostic commands, log interpretation, and resolution steps.

## Quick Diagnostic Commands

```powershell
# Check local environment
python --version
python -c "import src.job_automation"
python -m playwright --version

# Verify configuration files exist
Test-Path config/candidate_profile_config.json
Test-Path config/runtime/application.json
Test-Path config/credentials.json

# Check recent orchestration results
Get-Content output/orchestration_results.json | Select-Object -Last 20
Get-Content output/submission_log.json | Select-Object -Last 10

# VPS health check
pwsh scripts\check_vps_parallel_ats.ps1 -LogLines 50
```

## Import and Environment Issues

### `python` cannot import the package

**Symptoms:** `ModuleNotFoundError`, `ImportError`, or command not found.

**Resolution:**
1. Run commands from the repository root
2. Activate the virtual environment: `.\.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate` (Linux/Mac)
3. For installed command: `python -m pip install .`
4. For source-tree use: `python src/job_automation.py --help`

**Diagnostic:**
```powershell
pwd
Get-Command python
python -c "import sys; print(sys.path)"
```

### Configuration file missing or invalid

**Symptoms:** `FileNotFoundError`, JSON parse errors, schema validation failures.

**Resolution:**
1. Copy example templates:
   ```powershell
   Copy-Item config\candidate_profile_config.example.json config\candidate_profile_config.json
   Copy-Item config\candidate_email_pool.example.json config\candidate_email_pool.json
   ```
2. Validate JSON syntax: `python -c "import json; json.load(open('config/candidate_profile_config.json'))"`
3. Check required fields per [Configuration Guide](configuration.md)

## Browser Automation Issues

### Browser automation cannot start

**Symptoms:** Playwright errors, browser timeout, connection refused to CDP endpoint.

**Resolution:**
1. Install Chromium: `python -m playwright install chromium`
2. Check CDP endpoint in `config/runtime/browser.json` is running and reachable
3. Retry in `--headed` mode to visually inspect the provider page
4. Verify no other process is using the same browser profile directory

**Diagnostic:**
```powershell
python -m playwright install chromium --dry-run
Test-NetConnection -ComputerName localhost -Port <cdp_port>
Get-Process chrome -ErrorAction SilentlyContinue
```

### Form filling fails or hangs

**Symptoms:** Timeout errors, element not found, CAPTCHA detected.

**Resolution:**
1. Increase timeouts in `candidate_profile_config.json`: `navigation_timeout_ms`, `action_timeout_ms`
2. Use `--headed` to observe what the automation sees
3. Check for CAPTCHA: if detected twice within 24 hours, provider cooldown activates
4. Review `output/orchestration_results.json` for specific failure reason

**Logs to check:**
- Console output during the run
- `output/orchestration_results.json` - look for `status` field
- Screenshots (auto-deleted after attempt, but may be captured with `--headed`)

## Authentication and API Issues

### Gmail authorization fails

**Symptoms:** OAuth errors, 401/403 responses, token refresh failures.

**Resolution:**
1. Confirm `config/credentials.json` contains OAuth desktop-client credentials (not service account)
2. Verify the account has authorized the requested scopes in Google Cloud Console
3. Delete only `config/token.json` when reauthorization is intended (preserve credentials)
4. Check Gmail command exit status for OAuth/API error codes

**Diagnostic:**
```powershell
python -c "from google.oauth2.credentials import Credentials; print('OAuth lib OK')"
Test-Path config/credentials.json
Test-Path config/token.json
```

**Common error codes:**
- `invalid_client`: credentials.json is malformed or revoked
- `invalid_grant`: token expired, delete token.json to reauthorize
- `insufficient_scope`: account hasn't granted required permissions

### Vertex resume generation fails

**Symptoms:** AI model errors, permission denied, quota exceeded.

**Resolution:**
1. Check service-account file path in `config/runtime/vertex.json`
2. Verify project ID matches the service account's project
3. Confirm Vertex AI API is enabled in Google Cloud Console
4. Check service account has `roles/aiplatform.user` role
5. Review quota usage in Google Cloud Console

**Fallback:** The resume workflow has a rule-based fallback when AI is unavailable, but **always review its output before use**.

**Diagnostic:**
```powershell
Test-Path config/vertex_service_account.json
python -c "from google.auth import load_credentials_from_file; load_credentials_from_file('config/vertex_service_account.json')"
```

### VPS document archive cannot connect

**Symptoms:** SSH connection refused, host key verification failed, authentication failed.

**Resolution:**
1. Confirm PuTTY `plink` and `pscp` are on `PATH`
2. Verify `config/vps_config.json` has the correct dedicated archive user
3. Check `ssh_host_key` matches the trusted PuTTY-format fingerprint
4. If using `archive_private_key_file`, confirm it points to an existing dedicated `.ppk`

**Important:** An unknown or changed host key fails closed in batch mode. Verify changes independently instead of bypassing the pin.

**Diagnostic:**
```powershell
plink -V
Test-Path config/vps_config.json
# Manual SSH test (will prompt for confirmation)
plink <user>@<host> -hostkey <fingerprint> echo "connection ok"
```

## Application Submission Issues

### Application did not submit or queue stopped

**Symptoms:** Queue progress halted, no entry in submission log, uncertain confirmation.

**Resolution:**
1. Inspect `output/orchestration_results.json` for the specific engine result
2. Check `output/submission_log.json` for confirmed submissions
3. Review the provider page manually for confirmation message/email
4. Queue execution stops whenever return status or confirmation evidence is insufficient

**Critical:** Verify the employer's confirmation before retrying. Resume with the correct zero-based `--start-index` **only when duplicate submission is not possible**.

**Diagnostic commands:**
```powershell
# Check last orchestration result
Get-Content output/orchestration_results.json | ConvertFrom-Json | Select-Object -Last 1

# Check submission log
Get-Content output/submission_log.json | ConvertFrom-Json | Where-Object { $_.status -eq "SUBMITTED & CONFIRMED" } | Select-Object -Last 5

# Review queue progress
Get-Content output/job_url_queue_progress.json | ConvertFrom-Json
```

**Log interpretation:**
- `status: "SUBMITTED & CONFIRMED"` - success, recorded in submission log
- `status: "FILLED_NOT_SUBMITTED"` - form completed but no submission confirmed
- `status: "FAILED_REQUIRED_FIELD"` - missing answer in candidate profile
- `status: "CAPTCHA_DETECTED"` - manual review required, triggers cooldown
- `status: "TIMEOUT"` - increase timeouts in profile config

### Queue recovery procedure

When a queue stops mid-execution:

1. **Assess:** Check `output/job_url_queue_progress.json` for `last_index` and `last_url`
2. **Verify:** Manually confirm the status of the last attempted application
3. **Resume:** Use `--start-index` with the next zero-based index:
   ```powershell
   python src/job_automation.py queue --queue .\jobs.txt --start-index 3
   ```
4. **Prevent duplicates:** Never retry a URL without confirming it wasn't submitted

## Search and Discovery Issues

### Search returns too few results

**Symptoms:** Low job count, empty CSV, high exclusion rate.

**Resolution:**
1. Read `output/job_search_coverage.json` to understand filtering
2. Broaden location filters or use multiple locations
3. Provide known job boards via `--board-url` or `--boards-file`
4. Use multiple ATS platforms
5. Avoid unnecessarily narrow date filters (`--posted-since`, `--posted-until`)
6. Note: `--require-live` intentionally excludes roles whose liveness cannot be confirmed

**Diagnostic:**
```powershell
Get-Content output/job_search_coverage.json | ConvertFrom-Json
Get-Content output/ai_jobs.csv | Measure-Object -Line
```

**Coverage report fields:**
- `total_found`: raw count before filtering
- `after_dedup`: after removing duplicate URLs
- `after_liveness_check`: after verifying job is still open
- `final_count`: after all filters applied
- `backlog`: count of retained unsubmitted roles

### Job backlog is missing, stale, or unexpectedly retains a role

**Symptoms:** Backlog file absent, old timestamps, roles persist after closing.

**Resolution:**
1. Ensure VPS search includes `--backlog-output output/job_backlog.json`
2. Check the coverage report's `backlog` object for timestamp
3. Understand retention policy: timeouts, `429`, `5xx`, bot blocks, malformed responses, or uncertain page identity are **not** proof a job closed, so roles are intentionally retained

**Removal criteria:** Only an exact `SUBMITTED & CONFIRMED` ledger match or conclusive provider/page closure evidence removes a role from backlog.

**Pull synchronization:** The local pull requires coverage, current CSV, board cache, and backlog from one remote commit. If the remote generated-data branch predates backlog support, deploy current `main` and let one successful VPS search publish the first four-file snapshot.

**Diagnostic:**
```powershell
Get-Content output/job_search_coverage.json | ConvertFrom-Json | Select-Object -ExpandProperty backlog
Get-Content output/job_backlog.json | ConvertFrom-Json | Measure-Object
```

## VPS Operations Issues

### VPS ATS workers are active but making no progress

**Symptoms:** Worker services are running but their state and result timestamps do not advance.

**Resolution:**
1. Run status check: `pwsh scripts\check_vps_parallel_ats.ps1 -LogLines 120`
2. Compare:
   - Service state (`systemctl status job-app-*`)
   - Process list (Xvfb, Chromium, Python workers)
   - Repository commit (`git rev-parse HEAD`)
   - Worker state and result modification times
   - Recent service journals

**Common cause:** The VPS checkout or installed unit may point to older code or stale runtime configuration.

**Action:** Deploy the current `main` commit and verify the installed unit before retrying.

**Diagnostic script output interpretation:**
- `job-app-<ats> active (running)` ✓
- `Main PID: <pid>` - note for process inspection
- Artifact timestamps within expected window ✓

### Status helper times out

**Symptoms:** `check_vps_parallel_ats.ps1` exits without showing remote state.

**Resolution:**
1. Verify VPS provider status (running, not stopped/suspended)
2. Test SSH connectivity from provider console
3. Try alternative network path that can receive SSH banner
4. The helper exits after its configured timeout instead of leaving a hidden `plink` process

**Diagnostic:**
```powershell
# Test basic connectivity
Test-NetConnection <vps-ip> -Port 22

# From provider console, check SSH daemon
sudo systemctl status ssh

# Check firewall rules
sudo ufw status
```

### VPS services not starting on boot

**Symptoms:** Services inactive after reboot, manual start required.

**Resolution:**
1. Check service enablement: `systemctl is-enabled job-app-<ats>.service`
2. Enable if needed: `sudo systemctl enable job-app-<ats>.service`
3. Review journal for startup failures: `journalctl -u job-app-<ats>.service -n 50`
4. Verify dependencies (network, filesystem mounts) are available at boot time

## Archive and Document Issues

### Archive already exists with different content

**Symptoms:** Store operation fails with conflict error.

**Resolution:** Records are immutable for one canonical job URL and normalized email. A different company/title or PDF under that identity is reported as a conflict and is not overwritten.

**Action required:**
1. Verify the job URL and email are correct
2. Determine whether the existing record is the correct reviewed document set
3. If the new documents are correct, you must manually resolve on the VPS (this is intentional to prevent data loss)

**Never:** Do not attempt to bypass this check—it protects against accidental overwrites.

### Retrieved document fails verification

**Symptoms:** Retrieval completes but files not saved to output directory.

**Resolution:** No downloaded file is promoted when the manifest, identity, size, PDF signature, or SHA-256 check fails.

**Action:**
1. Preserve the local archive output directory
2. Inspect VPS archive and backups for corruption
3. **Do not use** the partial temporary download
4. Contact administrator if VPS-side corruption suspected

**Verification checks performed:**
- Manifest JSON structure and required fields
- Identity match (URL, company, title, email)
- File size matches manifest
- PDF signature validity
- SHA-256 hash matches manifest

## Getting More Help

If issues persist after following this guide:

1. **Gather diagnostics:**
   - Full command line used
   - Complete console output
   - Relevant JSON artifacts from `output/`
   - Configuration files (redact secrets)
   - VPS service status if applicable

2. **Check existing resources:**
   - [Configuration Guide](configuration.md) for setup validation
   - [Security and Privacy](security-and-privacy.md) for credential handling
   - [Operations Runbook](operations-runbook.md) for procedural guidance
   - [CLI Reference](cli-reference.md) for command options

3. **Review logs systematically:**
   - Start with `output/orchestration_results.json`
   - Check `output/submission_log.json` for confirmation state
   - Examine service journals on VPS: `journalctl -u job-app-<service> -n 100`

---

**See Also:**
- [FAQ](faq.md) - Frequently asked questions and common issues
- [Operations Runbook](operations-runbook.md) - Safe operating procedures
- [CLI Reference](cli-reference.md) - Command documentation
- [Configuration Guide](configuration.md) - Configuration options
