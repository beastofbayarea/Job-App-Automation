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

## Persistent parallel ATS workers

Install the continuous worker from Windows after the VPS checkout and private
candidate/Vertex/Gmail inputs are present:

```powershell
pwsh scripts\install_vps_continuous_ashby.ps1
pwsh scripts\install_vps_continuous_greenhouse.ps1
pwsh scripts\install_vps_continuous_lever.ps1
pwsh scripts\install_vps_continuous_smartrecruiters.ps1
pwsh scripts\install_vps_continuous_workable.ps1
```

These installations provide the systemd units `job-app-ashby.service`,
`job-app-greenhouse.service`, `job-app-lever.service`,
`job-app-smartrecruiters.service`, and `job-app-workable.service`.

Installing or repairing one provider does not stop or restart another. When
replacing an already-active instance of that same
provider, the installer waits for active `apply` subprocesses before restarting
it onto newly deployed code. The ignored candidate email pool is copied to the
VPS with mode `0600`. Each provider service runs headed Chromium under its own
Xvfb display, starts automatically on boot, and has `Restart=always`. The

Future ATS engines use the same provider-neutral unit without changes to the
supervisor:

```powershell
pwsh scripts\install_vps_continuous_ats.ps1 -AtsPlatform providername
```

The provider must have an installed engine module and search support for the
same lowercase platform name.

For two independent Greenhouse sources, run
`scripts/install_vps_greenhouse_excel_parallel.ps1`. It brings up the regular
search-backed Greenhouse worker and an Excel-tracker-backed Greenhouse worker,
coordinates both through shared provider job-ID claims, verifies them, and only
then disables `job-app-ashby.service`. Treat this as a deliberate alternative
topology; do not describe it as the all-provider parallel layout.

To run the three canonical Greenhouse workbooks concurrently, execute
`pwsh scripts/install_vps_greenhouse_excel_fleet.ps1`. The installer uploads
the ignored private workbooks, validates each tracker, creates isolated state,
selection, result, and document paths, and uses one shared claims file across
the three Excel workers and the existing search-backed Greenhouse worker. It
then disables `job-app-greenhouse-excel.service`,
`job-app-smartrecruiters.service`, and `job-app-workable.service`.

Each cycle selects exactly one unattempted, verified-live record for the
configured ATS from `output/continuous_<ats>_jobs.json`, initially seeded from
the latest `output/vps_generation_jobs.json`. It chooses a random email from
the configured pool, generates a job-specific PDF resume and one-page cover
letter, supplies both to the guarded orchestrator, and uploads the cover
letter whenever the ATS form exposes a matching file field. A result counts
only when both the strict engine result and
`output/submission_log.json` contain exact `SUBMITTED & CONFIRMED` evidence.
The worker waits a uniformly random 120-300 seconds after every cycle. When
the list is exhausted, it first refreshes its provider list from the newest
shared search-service snapshot, then falls back to a provider-only verified
search if the shared snapshot contains no unattempted work. That provider-only
refresh uses provider-specific CSV/cache/coverage files and merges its results
into the same locked active backlog.

Private state and evidence are kept in:

- `output/continuous_<ats>_jobs.json`
- `output/continuous_<ats>_state.json`
- `output/continuous_<ats>_results/`
- `output/continuous_<ats>_documents/`
- `output/submission_log.json`

The provider-specific lists prevent parallel refreshes from overwriting each
other. The shared submission log uses an interprocess lock and merges the
latest on-disk records before every atomic save.

The worker writes `application_started` before opening the live submission
boundary. If the process is interrupted after that checkpoint, the next
start changes the attempt to `manual_review` and never retries it. Required
field failures, CAPTCHA barriers, timeouts, and unconfirmed results likewise
never count as success or trigger an automatic retry. Two or more CAPTCHA
manual-review outcomes since the latest confirmation open a provider-wide
24-hour cooldown; the worker stays supervised but does not risk another live
submission until the cooldown expires.

#### Optional centralized worker telemetry

Install the optional SDK in the VPS environment with
`uv sync --locked --no-dev --extra observability`, then create the environment
file referenced optionally by every unattended worker unit:

```bash
sudo install -d -m 0700 /etc/job-application-automation
sudo install -m 0600 /dev/null /etc/job-application-automation/observability.env
sudoedit /etc/job-application-automation/observability.env
```

Set `SENTRY_DSN`, and optionally `SENTRY_ENVIRONMENT` and `SENTRY_RELEASE`, in
that file. Run `systemctl daemon-reload` and restart only the intended worker
units after reviewing the file permissions. Telemetry sends fixed operational
event names and the allow-list documented in
`docs/security-and-privacy.md`; it never sends candidate or job content,
exception messages, logs, paths, URLs, or screenshots. To disable it, remove
`SENTRY_DSN` (or the environment file) and restart the workers. A missing file,
missing SDK, initialization failure, or transport failure never stops a worker.

Inspect the service without starting another worker:

```powershell
pwsh scripts\check_vps_parallel_ats.ps1 -LogLines 120
```

The status probe includes capacity, enablement/activity for search and every
ATS service, worker processes, provider state summaries, and recent
`journalctl` output.

For a read-only inventory of everything that persists or wakes on a schedule on
the VPS—not only the job-application workers—run:

```powershell
pwsh scripts\audit_vps_runtime.ps1
```

The audit reports running and enabled services, timers, cron entries,
long-lived and high-resource processes, listening sockets, containers,
job-application unit resource counters, log usage, and reboot/update status.
It pins the configured SSH host key, redacts email addresses in command lines,
and does not start, stop, enable, or restart workloads.

Install or repair the dashboard as a loopback service behind the VPS's
validated Nginx configuration:

```powershell
pwsh scripts\install_vps_dashboard.ps1
```

The installer binds the Python server only to `127.0.0.1:8000` and restarts
Nginx only after `nginx -t` succeeds. It provisions no credentials.

**The dashboard is public and unauthenticated.** Nginx publishes it to the open
internet, and every route — KPI metrics, the submission log, the raw file
inspector under `/api/files/`, generated resumes and cover letters under
`/api/download/`, and the sync log at `/api/vps/log` — is readable by anyone
with the URL, including search-engine crawlers. Treat anything reachable from
`output/` as published. Before deploying, confirm that directory holds nothing
you would not post publicly.

The server exposes no write or command-executing routes: `POST` returns `404`
for every path. Report syncing and VPS status checks are operator tasks, run
from a shell on your workstation:

```powershell
pwsh scripts\pull_vps_application_reports.ps1 -Overwrite
pwsh scripts\check_vps_parallel_ats.ps1 -LogLines 50
```

If the shared VPS's Cent Capital backend is active but not listening on port
8080 because its journal shows rejected database credentials, quarantine that
restart loop with:

```powershell
pwsh scripts\quarantine_unhealthy_cent_backend.ps1
```

The command refuses to act if the backend is already inactive, is listening,
or lacks explicit database-authentication failure evidence. It does not alter
the environment file; a future deployment must provide valid replacement
credentials before re-enabling the backend.

For a low-memory VPS running multiple headed browser workers, install bounded
swap headroom once:

```powershell
pwsh scripts\install_vps_memory_guard.ps1
```

The idempotent guard creates a 2 GiB `/swapfile` only when no swap is active,
requires at least 1 GiB of disk headroom beyond the requested swap, refuses to
overwrite an unrelated `/swapfile`, persists the mount in `/etc/fstab`, and
sets `vm.swappiness=10`. Use `-SwapSizeMiB` to select 512–4096 MiB.

Private application reports are pulled separately over pinned SSH:

```powershell
pwsh scripts\pull_vps_application_reports.ps1
pwsh scripts\pull_vps_application_reports.ps1 -Overwrite
```

The command atomically downloads and validates `submission_log.json` and
`vps_application_failures.json` into `output/vps_reports/`. Without
`-Overwrite`, either existing local file prevents the operation before the VPS
is contacted.

Check a live run without acquiring its lock or starting another workflow:

```powershell
pwsh scripts\check_vps_parallel_ats.ps1 -LogLines 120
```

This read-only command has a bounded SSH timeout and prints remote clock and
uptime, supervised ATS unit state, self-filtered automation processes,
repository commit/state, worker-state timestamps, and recent ATS journals.

Generated resume and cover-letter cleanup is dry-run by default:

```powershell
pwsh scripts\prune_old_outputs.ps1
```

Add `-Delete` only after reviewing the listed files. Keep
`config/vps_config.json` restricted and out of Git. Private document operations
require host-key pinning and support a dedicated archive key.

---

**See Also:**
- [Quick Reference](quick-reference.md) - Common commands and examples
- [CLI Reference](cli-reference.md) - Complete command documentation
- [Configuration Guide](configuration.md) - Configuration options
- [FAQ](faq.md) - Frequently asked questions
- [Troubleshooting Guide](troubleshooting.md) - Issue resolution
