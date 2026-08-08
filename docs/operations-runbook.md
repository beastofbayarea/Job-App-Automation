# Operations Runbook

This document provides safe operating procedures for the Job Application Automation toolkit. For quick command examples, see [Quick Reference](quick-reference.md). For troubleshooting, see [Troubleshooting Guide](troubleshooting.md).

## Safe operating sequence


## Safe operating sequence

1. Update the candidate profile, email pool, and approved resume source.
2. Search public boards and inspect `output/ai_jobs.csv`,
   `output/job_backlog.json`, and `output/job_search_coverage.json`.
3. Generate and review a tailored resume or cover letter.
4. Run `apply` with `--dry-run` or `--fill-only` first.
5. Review browser evidence and the orchestration result before using `--live-submit`.
6. Confirm that a live submission is recorded in `output/submission_log.json`.
7. Archive the exact reviewed CV and cover letter with `documents store --execute`, or use `documents generate --archive` for a prepared pair.

`apply` is dry-run by default. A filled form is not a confirmed submission. The queue command is different: it always requests live submission and must be used only for intentional, reviewed queues.

## Search and review

```powershell
python src/job_automation.py search --role-type "Product Manager" --ats-platform greenhouse --verify-live --require-live
```

Use `--board-url`, `--boards-file`, `--career-page`, or `--career-pages-file` for known sources. Use explicit `--posted-on`, `--posted-since`, and `--posted-until` filters when a calendar window matters. Read the coverage report before treating a low result count as absence of roles.

## Application workflow

```powershell
# Safe workflow check for one known job.
python src/job_automation.py apply --url "https://…" --company "Example" --role "Product Manager" --dry-run

# Drive the form without submitting, then inspect the result and visible browser state.
python src/job_automation.py apply --url "https://…" --company "Example" --role "Product Manager" --fill-only --headed
```

For a reviewed live run, replace the mode with `--live-submit`. Use `--results-file` and `--submission-log-file` to retain separate evidence for a campaign. URL mode ignores `--tracker`; tracker mode uses the configured spreadsheet and accepts `--limit`, `--start-index`, and `--no-shuffle`.

## Queue recovery

The queue contains one non-empty URL per line. It stops on the first unconfirmed result and writes `output/job_url_queue_progress.json`, including `last_index`, `last_url`, confirmation state, and the engine result.

Do not blindly rerun a failed queue. First inspect the provider page, generated result, and submission log. When the previous entry was not submitted, resume with the next intended zero-based index:

```powershell
python src/job_automation.py queue --queue .\jobs.txt --start-index 3
```

If submission status is uncertain, verify it with the employer account or confirmation email before retrying. This avoids duplicate applications.

## Operational artifacts

- `output/orchestration_results.json`: result records from an application run.
- `output/submission_log.json`: only confirmed submissions recorded by the orchestrator.
- `output/job_url_queue_progress.json`: latest queue checkpoint.
- `output/ai_jobs.csv` and `output/job_search_coverage.json`: search results and coverage evidence.
- `output/job_backlog.json`: persistent active, unsubmitted public job metadata.
- `output/google_url_submission_report.json`: latest sitemap, URL notification, or
  notification-status result.

All persisted artifacts use an atomic replace, so a completed write is not partially visible.

## Google sitemap and eligible URL submission

`google-indexing` reads the published-site property from
`config/seo_config.json` and the Search Console/Indexing service-account role
from the ignored `config/cent_capital_config.json`. Before live use, enable the
Google Indexing API and Search Console API in the cloud project, then add the
exact `search_console_indexing.email` identity as a delegated owner of the
configured Search Console property.

Validate the complete configuration linkage without a Google API mutation:

```powershell
python src/job_automation.py google-indexing sitemap --dry-run
```

Submit the general site sitemap:

```powershell
python src/job_automation.py google-indexing sitemap
```

Direct URL notifications are narrower. Configure or pass only an owned page
that contains `JobPosting` JSON-LD or a `BroadcastEvent` nested in a
`VideoObject`. The command fetches and checks every page before authenticating,
and validates the complete batch before sending its first notification:

```powershell
python src/job_automation.py google-indexing submit `
  --url "https://skybison.cloud/jobs/example" `
  --type URL_UPDATED `
  --dry-run
```

For `URL_DELETED`, remove the page first or add `noindex`; a live `200` page
without `noindex` is rejected. Do not place the general dashboard URLs in
`eligible_urls`; Google supports those through the sitemap, not through its
direct Indexing API. The default initial publish quota is 200 URL notifications
per project per day, and this project additionally caps a single command by the
configured `batch_size`. Do not retry ambiguous failures blindly. Inspect the
atomic `output/google_url_submission_report.json` and query the read-only status
operation before deciding whether another notification is appropriate.

## Private VPS document archive

Do not place the private archive root below the repository clone, a Git worktree, `public_html`, `www`, or another web root.

One-time VPS setup should use a dedicated unprivileged SSH account. As an administrator on the VPS:

```bash
sudo install -d -m 0700 -o jobarchive -g jobarchive \
  /var/lib/job-application-automation/private-archive
```

Pin the VPS host-key fingerprint in the ignored `config/vps_config.json`, and preferably configure a dedicated PuTTY private key. Verify a local upload plan before any live transfer:

```powershell
python src/job_automation.py documents store `
  --url "https://jobs.example.com/role-id" `
  --company "Example" `
  --job-title "Product Manager" `
  --email "candidate@example.com" `
  --cv ".\output\reviewed-resume.pdf" `
  --cover-letter ".\output\reviewed-cover-letter.pdf"
```

Add `--execute` only after checking the displayed archive ID and hashes. The remote commit uses a private `.incoming` directory, verifies sizes and SHA-256 digests, and atomically promotes a new immutable record. An identical retry returns `ALREADY_STORED`; different content for the same job URL and email fails as a conflict.

Retrieval requires all four selectors and downloads both PDFs:

```powershell
python src/job_automation.py documents retrieve `
  --url "https://jobs.example.com/role-id" `
  --company "Example" `
  --job-title "Product Manager" `
  --email "candidate@example.com"
```

The default destination is `output/retrieved_documents/<archive-id>/`. Existing files are not replaced unless `--overwrite` is explicit. Keep encrypted/offsite backups of the private archive; SSH permissions do not protect against VPS disk loss.
