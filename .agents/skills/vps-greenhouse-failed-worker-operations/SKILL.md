---
name: vps-greenhouse-failed-worker-operations
description: Diagnose, repair, deploy, and safely rerun jobs handled by the role-specific Greenhouse failed-JSON systemd workers on the VPS. Use when checking those service states, investigating failed or timed-out jobs, authorizing corrected retries, restarting affected workers, or verifying the post-retry confirmation ledger.
---

# VPS Greenhouse Failed Worker Operations

Treat service health, job outcome, deployment state, and live application health as separate facts. Preserve exact confirmation-ledger semantics throughout recovery.

## Recover failed jobs

1. Establish local and remote state before changing anything.

   ```powershell
   git status --short
   pwsh scripts/audit_vps_runtime.ps1 -LogLines 40 -TimeoutSeconds 45
   pwsh scripts/check_vps_automation_status.ps1 -LogLines 40 -TimeoutSeconds 45
   pwsh scripts/check_vps_parallel_ats.ps1 -LogLines 40 -TimeoutSeconds 45
   pwsh scripts/audit_vps_greenhouse_failed_json_retry_queue.ps1 -SummaryOnly
   pwsh scripts/inspect_vps_greenhouse_failed_json_fleet.ps1
   ```

   Record unit enablement, `ActiveState`, `SubState`, `NRestarts`, process count, claims, per-role state, recent journals, host capacity, VPS revision, and worktree status. An active idle service does not prove that prior applications succeeded.

2. Classify each failed record using its state artifact, engine result, exact job URL identity, and `output/submission_log.json`.

   - Never retry exact confirmed-ledger matches.
   - Quarantine `manual_review`, `SUBMISSION_UNCONFIRMED`, `SUBMIT_ATTEMPT_UNCONFIRMED`, and `INTERRUPTED_AFTER_APPLICATION_START`.
   - Treat historical `TIMED_OUT` outcomes as ambiguous unless retained evidence independently proves the submission boundary was never reached. The targeted requeue tool intentionally rejects timeouts.
   - Require a terminal `failed` record, an allowlisted pre-submit result, `submitted=false`, no confirmation evidence, a recently verified live job-specific listing, and fewer than two fixing attempts.
   - Obtain legal, eligibility, and profile-backed answers only from verified candidate configuration; never infer them.

3. Reproduce the concrete failure from retained labels, controls, screenshots, engine JSON, and diagnostics. Fix the narrow cause, add regression tests, run focused tests and linting, then commit and push. Do not use an unrelated runtime-fingerprint change as retry authorization.

4. Deploy the pushed revision and refresh the ignored candidate profile when the fix depends on profile-backed answers.

   ```powershell
   pwsh scripts/deploy_vps_code.ps1 -TimeoutSeconds 120
   pwsh scripts/sync_vps_candidate_profile.ps1 -TimeoutSeconds 60
   ```

   Verify the VPS revision after deployment. Preserve unrelated remote changes; never force-reset the checkout.

5. Authorize only the reviewed tuples. Use the five hyphenated role names accepted by the script.

   ```powershell
   $targets = @(
       "core-product-management|Example Company|Product Manager|https://job-boards.greenhouse.io/example/jobs/123456"
   )
   pwsh scripts/requeue_vps_greenhouse_failed_json_targets.ps1 `
       -Target $targets `
       -TimeoutSeconds 120
   ```

   The command matches the exact URL identity, validates the entire batch before mutation, checks the exact ledger, refuses to interrupt active application or document-generation processes, backs up state and claims, sets one-shot `retry_authorized=true`, and restarts only affected units. If any tuple is unsafe or missing, correct the selection; do not fall back to the fleet-wide requeue.

6. Monitor until every authorized claim reaches a terminal state.

   ```powershell
   pwsh scripts/audit_vps_greenhouse_failed_json_retry_queue.ps1 -SummaryOnly
   pwsh scripts/check_vps_automation_status.ps1 -LogLines 60 -TimeoutSeconds 45
   pwsh scripts/inspect_vps_greenhouse_failed_json_fleet.ps1
   ```

   Diagnose any new failure from its newly retained evidence before another authorization. Never exceed the two-fixing-attempt ceiling. Restart the full five-role fleet only after there are no in-flight claims.

7. Finish with evidence: local, origin, and VPS revisions; clean tracked worktrees; all five unit states and start timestamps; authorized/claimed/awaiting counts; exact confirmed-ledger changes; and the final outcome of every requested tuple.
