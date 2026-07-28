# VPS Sync Maintenance Scripts

> **Historical design record (implemented 2026-07-28).** Use
> `docs/operations-runbook.md` for current operating instructions.

Date: 2026-07-28

## Context

The repo already has a VPS→local sync pipeline: `scripts/vps_search_sync.sh` runs
on a cron on the Hostinger VPS, executes `job_automation.py search`, and pushes
sanitized output (`ai_jobs.csv`, `job_search_coverage.json`, `ats_boards_cache.json`)
to the `vps-search-output` branch. `scripts/pull_search_output.ps1` pulls those
files into the local `output/` folder as one commit-coherent snapshot. The
dedicated output branch intentionally has unrelated history and is never merged
into `main`.

This spec adds maintenance and safety controls around that pipeline.

## 1. `scripts/trigger_vps_search.ps1`

Runs an out-of-cycle search instead of waiting for the daily 3am cron.

- Reads `host`, `ssh_user`, `ssh_password.value` from `config/vps_config.json`.
- Uses `plink.exe` (PuTTY, already installed locally) with a per-run `-pwfile`
  that is removed in `finally`; the plaintext password is not placed directly
  in the Plink process arguments.
- Requires an absolute POSIX `-RemoteRepoPath`, encodes its script path as one
  POSIX shell literal, and runs `exec bash -- <quoted-script-path>` remotely.
- The VPS-side script takes a nonblocking `flock` before search or publication,
  so a manual run cannot overlap the cron run.
- Publication fails before touching the sync worktree if the search did not
  produce all three required artifacts, preventing mixed-run output.
- Streams stdout/stderr so failures are visible immediately.
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
- Compares the unrounded age and rounds only display text. Timestamps more than
  five minutes in the future fail by default; `-ClockSkewMinutes` adjusts that
  tolerance.
- Prints `OK (age: Xh)` or `STALE (age: Xh, threshold: Nh)`.
- Exit code 0 when fresh, 1 when stale, malformed, future-dated, or missing.

## 3. `scripts/prune_old_outputs.ps1`

Cleans up accumulated `output/*_Resume.pdf` and `output/*_Cover_Letter.pdf`
files.

- Age cutoff: 14 days, overridable via `-Days`.
- Negative ages are rejected, and `-OutputDir` is resolved as a literal
  directory rather than as a wildcard.
- Dry-run by default: lists matching files older than the cutoff and total
  reclaimable size, deletes nothing.
- `-Delete` switch performs the actual deletion.

## 4. Coherent local pulls

- `scripts/pull_search_output.ps1` resolves the repository independently of the
  caller's current directory.
- All three published files must exist in the same fetched commit before any
  local file is replaced.
- `git restore --worktree` reads that one commit without checking out or staging
  the generated artifacts on `main`.

## 5. Log rotation for `vps_sync.log`

- New file `scripts/vps-sync.logrotate`: rotates `vps_sync.log` weekly, keeps
  4 rotations, and compresses old ones. It is a template rather than a
  hard-coded repository path.
- `scripts/install_vps_logrotate.sh` derives the absolute repository path,
  renders the template, and installs it with mode `0644`. `--stdout` renders it
  without changing the VPS.
- One-time manual setup documented in a comment block added to the top of
  `scripts/vps_search_sync.sh` (same convention already used there for the
  deploy-key setup): `bash scripts/install_vps_logrotate.sh`.
- Not installed automatically — this is a manual step run once on the VPS.

## Out of scope

- Rotating/hardening the plaintext VPS root password in `config/vps_config.json`
  — flagged separately, explicitly deferred by the user for this round.
- Any change to the search/resume/cover-letter generation logic itself.

## Testing

- Offline regression tests mock Plink, verify shell-literal round trips, ensure
  the password file is removed after failure, and never contact the VPS.
- Freshness tests cover the rounding boundary, future timestamps, and a fresh
  report. Pruning tests cover negative ages, literal paths, and dry-run safety.
- Pull tests use a local bare Git remote to verify complete and incomplete
  snapshots, index isolation, and invocation from another working directory.
- Bash parsing and logrotate template rendering run locally. The `flock`
  concurrency and missing-publisher-artifact tests run where `flock` is
  available.
- Logrotate config: syntax-checked with `logrotate -d` when installed on the VPS
  (manual, by the user).
