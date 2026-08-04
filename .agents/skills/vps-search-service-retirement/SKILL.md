---
name: vps-search-service-retirement
description: Audit and permanently remove the Job App Automation continuous VPS search service, its scheduling hooks, credentials, sync worktree, generated search artifacts, and executable wrappers. Use when asked to delete, uninstall, retire, or verify removal of job-app-search-sync.service without disrupting separate ATS application workers.
---

# VPS Search Service Retirement

Retire only the continuous search service. Preserve unrelated ATS application
workers, submission ledgers, generated application documents, candidate data,
dashboard services, and shared environment configuration.

## Known service footprint

- Unit: `job-app-search-sync.service`
- Unit file: `/etc/systemd/system/job-app-search-sync.service`
- Working tree: `/root/Job-App-Automation`
- Executable: `scripts/vps_continuous_search_sync.sh`
- Search/publish implementation: `scripts/vps_search_sync.sh`
- Private deploy key: `/root/.ssh/vps_search_sync` and `.pub`
- Publication worktree: `.sync-worktree`
- Lock: `.git/vps-search-sync.lock`
- Search runtime artifacts:
  `output/vps_sync.log`, `vps_run_status.json`, `job_search_coverage.json`,
  `ai_jobs.csv`, `ats_boards_cache.json`, `job_backlog.json`, and
  `vps_generation_jobs.json`
- Historical scheduling markers:
  `job-app-automation-daily-search`, `vps_search_sync`, and
  `job-app-search-sync`

## Procedure

1. Run `scripts/remove_vps_search_service.ps1 -AuditOnly` from the local repo.
   Capture `systemctl show`, `systemctl cat`, every resolved related path,
   matching cron lines, and matching processes.
2. Confirm the resolved repository is exactly the intended absolute VPS path.
   Never delete a computed broad directory, the repo root, `output/` itself,
   `/root`, or `.ssh`; delete only the enumerated paths.
3. Run `scripts/stop_vps_search_service.ps1` if the unit is active. The script
   bounds graceful shutdown and escalates only within the target unit.
4. Run `scripts/remove_vps_search_service.ps1`. It disables and stops the unit,
   removes the unit file, filters only matching cron hooks, removes the private
   deploy-key pair, prunes the sync worktree, deletes the named lock/artifacts
   and VPS executable wrappers, clears staged unit files, and reloads systemd.
5. Run the audit command again in a fresh SSH session. Require:
   - `LoadState=not-found`, `ActiveState=inactive`, and `MainPID=0`;
   - no related paths listed;
   - no matching cron entries;
   - no search-sync, search-document, or search-application process.
6. Check the local repository for unexpected changes. VPS deletion of tracked
   executable wrappers intentionally dirties only the VPS checkout; do not
   mistake that for a local source deletion.
7. Report exactly what was removed and explicitly state that unrelated ATS
   workers and protected application data were preserved.

## Verification caveat

Exclude the remote teardown shell's own PID from process matching. Its command
line contains the target filenames and can otherwise create a false
`SEARCH_PROCESS_STILL_PRESENT` result even after deletion succeeds.

## Recovery

This retirement is intentionally destructive on the VPS. The systemd unit and
executable wrappers remain reproducible from repository history, but the
deleted private deploy key and generated search artifacts are not recovered by
reinstalling the unit. A new deploy key must be generated and authorized before
restoring publication access.
