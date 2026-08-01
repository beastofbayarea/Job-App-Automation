# Scripts

Executable entry points stay in this directory so documented commands and VPS
automation paths remain stable. Supporting files are separated from those commands:

- `lib/` contains shared PowerShell functions. It is sourced by entry points and is
  not intended to be run directly.
- `templates/` contains systemd units and the logrotate policy installed by the VPS
  setup scripts.

## Entry-point groups

- `install_vps_*` installs or repairs VPS services and scheduled operations.
- `check_*` and `audit_*` perform read-only health and state checks.
- `deploy_*`, `trigger_*`, `retry_*`, and `quarantine_*` perform explicit VPS actions.
- `restart_vps_runtime.ps1` validates the deployed split configuration, then
  restarts and verifies all repository-backed VPS services.
- `pull_*`, `cleanup_*`, and `prune_*` maintain local or remote artifacts.
- `vps_*.sh` implements the persistent and scheduled VPS runtime workflows.
- `cleanup_job_url_workbooks.mjs` and `create_new_ats_tracker.py` maintain job-source
  workbooks.

Run entry points from the repository root unless their help text says otherwise. See
[`docs/operations-runbook.md`](../docs/operations-runbook.md) for supported commands
and operational safeguards.
