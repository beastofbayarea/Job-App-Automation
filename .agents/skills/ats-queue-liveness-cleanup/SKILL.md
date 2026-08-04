---
name: ats-queue-liveness-cleanup
description: Audit, back up, and prune closed or unconfirmed job records from Greenhouse, Workable, SmartRecruiters, Lever, and Ashby JSON application queues. Use when asked to check whether queued ATS URLs are live, delete stale jobs, remove unsure records, preserve a newly created search file, or repeat the five-ATS cleanup workflow.
---

# ATS Queue Liveness Cleanup

Verify queue records with authoritative ATS APIs, retain recoverable backups,
and remove only the records authorized by the user's liveness standard.

## Workflow

1. Enumerate JSON files in each requested queue folder. Identify files the user
   excluded, especially a newly created dated search artifact. Never pass those
   files to a cleanup script.
2. Inspect the record schema and URL field before running anything. Operational
   queue records normally use `job_url`; fresh search artifacts use `url`.
3. Run a dry audit first. Use `--remove-unsure` when the user requests deletion
   of closed *and* unsure jobs. Without it, preserve unknown results.
4. Treat only authoritative evidence as live:
   - Greenhouse: individual Job Board API response with matching numeric ID and
     an unexpired application deadline.
   - Lever: individual postings API response with matching UUID.
   - SmartRecruiters: individual public posting API response with matching ID.
   - Ashby: matching listed UUID in the current employer board response.
   - Workable: matching shortcode in the current account feed. Resolve `/j/`
     short URLs through the HTML canonical account link first.
5. Do not treat a transient request failure as final on the first pass. Retry at
   lower concurrency (3–4 workers and a 30-second timeout). Under strict mode,
   delete only results still unknown after that stable retry.
6. Review status/reason counts before applying. Confirm that malformed URLs,
   404/410 responses, missing board entries, expired deadlines, and persistent
   unknowns match the requested removal policy.
7. Apply using the same stable settings. Each script writes a timestamped backup
   under `output/` before atomically replacing its input JSON.
8. Run the same command again without `--apply`. Require zero removals and all
   retained records to report live. Confirm excluded files were untouched.
9. Run focused tests and `git diff --check`. Commit and push reusable script or
   skill improvements; respect ignore rules for private queue data.

## Commands

Run from the repository root. Add `--apply` only after reviewing the dry run.

```powershell
# Greenhouse folder; repeat --exclude-name when needed
python scripts/prune_inactive_greenhouse_failed_json.py `
  --data-dir data/application-queues/greenhouse `
  --exclude-name greenhouse-job-search-YYYY-MM-DD.json `
  --remove-unsure --workers 4 --timeout-seconds 30

# Workable or SmartRecruiters
python scripts/prune_inactive_smartrecruiters_workable_json.py `
  --platform workable `
  --input data/application-queues/workable/product-management.json `
  --remove-unsure --workers 3 --timeout-seconds 30

# Lever
python scripts/prune_inactive_lever_failed_json.py `
  --input data/application-queues/lever/product-management.json `
  --remove-unsure --workers 8 --timeout-seconds 30

# Ashby
python scripts/prune_inactive_ashby_failed_json.py `
  --input data/application-queues/ashby/product-management.json `
  --remove-unsure --workers 4 --timeout-seconds 30
```

## Safety and reporting

- Resolve exact input and exclusion paths before any write.
- Never include dated search files merely because they share the folder.
- Prefer a successful lower-concurrency retry over deleting transient failures.
- Report checked, live, closed, unknown, and removed counts per ATS.
- Report backup paths and whether the post-cleanup audit reached zero removals.
