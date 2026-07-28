# VPS Sync Maintenance Scripts

Date: 2026-07-28

## Context

The repo already has a VPS→local sync pipeline: `scripts/vps_search_sync.sh` runs
on a cron on the Hostinger VPS, executes `job_automation.py search`, and pushes
sanitized output (`ai_jobs.csv`, `job_search_coverage.json`, `ats_boards_cache.json`)
to the `vps-search-output` branch. `scripts/pull_search_output.ps1` pulls those
files into the local `output/` folder (recently fixed so a missing file no
longer blocks the others from being pulled).

This spec adds four small, independent maintenance scripts around that pipeline.

## 1. `scripts/trigger_vps_search.ps1`

Runs an out-of-cycle search instead of waiting for the daily 3am cron.

- Reads `host`, `ssh_user`, `ssh_password.value` from `config/vps_config.json`.
- Uses `plink.exe` (PuTTY, already installed locally) with `-pw` for
  non-interactive password auth — Windows OpenSSH has no built-in
  non-interactive password mode.
- Runs `bash <repo-path-on-vps>/scripts/vps_search_sync.sh` remotely via plink,
  streaming stdout/stderr so failures are visible immediately.
- On success (remote exit code 0): automatically invokes
  `scripts\pull_search_output.ps1` locally.
- On failure: stops, prints the remote exit code, does **not** attempt a pull
  (avoids pulling stale/partial data).
- Remote repo path is a script parameter (`-RemoteRepoPath`) since it isn't
  currently recorded anywhere in the repo.

## 2. `scripts/check_sync_freshness.ps1`

Reports whether the local `output/job_search_coverage.json` reflects a recent
VPS run.

- Reads the coverage report's generation timestamp field.
- Threshold: 24 hours, overridable via `-ThresholdHours`.
- Prints `OK (age: Xh)` or `STALE (age: Xh, threshold: Nh)`.
- Exit code 0 when fresh, 1 when stale or the file is missing.

## 3. `scripts/prune_old_outputs.ps1`

Cleans up accumulated `output/*_Resume.pdf` and `output/*_Cover_Letter.pdf`
files.

- Age cutoff: 14 days, overridable via `-Days`.
- Dry-run by default: lists matching files older than the cutoff and total
  reclaimable size, deletes nothing.
- `-Delete` switch performs the actual deletion.

## 4. Log rotation for `vps_sync.log`

- New file `scripts/vps-sync.logrotate`: rotates `vps_sync.log` weekly, keeps
  4 rotations, compresses old ones.
- One-time manual setup documented in a comment block added to the top of
  `scripts/vps_search_sync.sh` (same convention already used there for the
  deploy-key setup): `sudo cp scripts/vps-sync.logrotate /etc/logrotate.d/vps-sync`.
- Not installed automatically — this is a manual step run once on the VPS.

## Out of scope

- Rotating/hardening the plaintext VPS root password in `config/vps_config.json`
  — flagged separately, explicitly deferred by the user for this round.
- Any change to the search/resume/cover-letter generation logic itself.

## Testing

- `trigger_vps_search.ps1`: dry-run against real VPS once `-RemoteRepoPath` is
  confirmed; verify it correctly no-ops the pull on a simulated remote failure.
- `check_sync_freshness.ps1`: test against current `output/job_search_coverage.json`
  and against a missing/absent file.
- `prune_old_outputs.ps1`: test dry-run output against current `output/` (~100
  files from 2026-07-27), confirm no deletion without `-Delete`.
- Logrotate config: syntax-checked with `logrotate -d` when installed on the VPS
  (manual, by the user).
