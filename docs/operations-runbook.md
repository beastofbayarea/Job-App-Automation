# Operations runbook

## Safe operating sequence

1. Update the candidate profile, email pool, and approved resume source.
2. Search public boards and inspect `output/ai_jobs.csv` plus `output/job_search_coverage.json`.
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

# Drive the form without submitting, then inspect the result and screenshots.
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

This archive is separate from VPS search synchronization. Do not place its root below the repository clone, a Git worktree, `public_html`, `www`, or another web root.

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

## VPS search synchronization

### Persistent parallel ATS workers

Install the continuous worker from Windows after the VPS checkout and private
candidate/Vertex/Gmail inputs are present:

```powershell
pwsh scripts\install_vps_continuous_search.ps1
pwsh scripts\install_vps_continuous_ashby.ps1
pwsh scripts\install_vps_continuous_greenhouse.ps1
pwsh scripts\install_vps_continuous_lever.ps1
```

These installations replace the marked daily cron entry with the systemd
units `job-app-search-sync.service`, `job-app-ashby.service`,
`job-app-greenhouse.service`, and `job-app-lever.service`. All four run in
parallel. The search service continuously refreshes verified job discovery,
publishes only the safe coverage/jobs/board-cache snapshot, waits five minutes,
and repeats. It does not generate documents or submit applications.

Installing or repairing one provider does not stop or restart another or the
search service. When replacing an already-active instance of that same
provider, the installer waits for active `apply` subprocesses before restarting
it onto newly deployed code. The ignored candidate email pool is copied to the
VPS with mode `0600`. Each provider service runs headed Chromium under its own
Xvfb display, starts automatically on boot, and has `Restart=always`. The
search-only service does not launch a browser and runs with lower CPU, memory,
I/O, and process limits so submission workers retain priority.

Future ATS engines use the same provider-neutral unit without changes to the
supervisor:

```powershell
pwsh scripts\install_vps_continuous_ats.ps1 -AtsPlatform providername
```

The provider must have an installed engine module and search support for the
same lowercase platform name.

Each cycle selects exactly one unattempted, verified-live record for the
configured ATS from `output/continuous_<ats>_jobs.json`, initially seeded from
the latest `output/vps_generation_jobs.json`. It chooses a random email from
the configured pool, generates a job-specific PDF resume and one-page cover
letter, supplies both to the guarded orchestrator, and uploads the cover
letter whenever the ATS form exposes a matching file field. A result counts
only when both the strict engine result and
`output/submission_log.json` contain exact `SUBMITTED & CONFIRMED` evidence.
The worker waits a uniformly random 120-300 seconds after every cycle. When
the list is exhausted, it refreshes verified results for the selected ATS and
continues.

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
never count as success or trigger an automatic retry.

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
pwsh scripts\check_vps_automation_status.ps1 -LogLines 50
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

The `vps-search-output` branch is a dedicated generated-data branch with
unrelated history. Keep it separate from `main`; use the synchronization
scripts instead of merging it.

On the VPS, install the repository-path-aware logrotate policy once:

```bash
bash scripts/install_vps_logrotate.sh
```

Alternatively, install or repair the cron entry, log rotation, and private
archive directory together from Windows:

```powershell
pwsh scripts\install_vps_daily_automation.ps1 `
  -RemoteRepoPath /absolute/path/to/Job-App-Automation
```

The default schedule is 03:00 UTC daily; use `-HourUtc` to select another UTC
hour. The operation is idempotent and replaces only the cron line marked
`job-app-automation-daily-search`.

After search and liveness verification, the scheduled workflow first publishes
the complete coverage, jobs, and board-cache snapshot. Private document work
cannot delay that publication. It then generates and archives a bounded set of
CV/cover-letter pairs: `application.vps_max_document_jobs` controls the total
per run (10 by default), while `application.vps_document_retry_jobs` reserves
part of that capacity for prior failures (two by default). Remaining capacity
advances new jobs, so permanent failures cannot starve the backlog. Archived
URLs are skipped.

Individual document failures keep the final cron status nonzero and remain
visible in `output/vps_sync.log`, but they do not suppress the guarded
application stage for other jobs with archived pairs.

The installer also transfers the candidate profile, resume source, Vertex
credential, and pre-authorized Gmail OAuth credential/token needed by
unattended resume generation and Greenhouse verification. These files are
stored with mode `0600`. Complete Gmail authorization locally before running
the installer; cron cannot complete an interactive OAuth browser flow.

The installer also provisions `xvfb` if missing. The application-stage
engines always launch a headed (non-headless) Chrome so ATS anti-bot checks
cannot fingerprint a headless browser; `vps_search_sync.sh` transparently
re-execs itself under `xvfb-run` when no `DISPLAY` is set, so headed Chrome
still launches on a display-less VPS instead of crashing with "Missing X
server or $DISPLAY".

### Automatic VPS application stage

After publishing the safe search snapshot and completing the bounded document
stage, the daily workflow invokes the guarded application runner. Only complete
`live` records for Greenhouse, Lever, and Ashby with an `archived` entry in
`output/vps_document_archive_state.json` are eligible. The runner calls the
existing orchestrator with `--live-submit`, processes records sequentially, and
uses `application.vps_max_attempts_per_ats` as its per-provider limit.

`output/vps_application_state.json`,
`output/vps_application_results/`, `output/submission_log.json`, and ATS
screenshots are private VPS artifacts. They must never be added to
`vps-search-output`. A prior exact `SUBMITTED & CONFIRMED` log entry is skipped.
Every attempted job is recorded atomically.

Any CAPTCHA, required-field failure, timeout, malformed result, engine error,
or submission lacking exact confirmation is saved as `failed`. The runner
prints the failure and continues with the remaining eligible roles, including
later roles on the same ATS. It returns nonzero after completing the lists when
one or more failures occurred, keeping cron visibly unhealthy without hiding
successful applications.

Inspect `output/vps_application_failures.json` for the URL, ATS, company, role,
exit code, result status, error/detail text, missing fields, stdout/stderr
tails, and per-job evidence path. Failed URLs remain skipped on later runs to
avoid duplicate submissions. If a reviewed failure is definitely safe to
retry, remove only that URL's entry from
`output/vps_application_state.json`; never clear ambiguous state merely to
increase throughput.

The cron entry and an on-demand trigger both run
`scripts/vps_search_sync.sh`. That script uses a nonblocking lock and exits
without starting when another sync is active. It also refuses to publish unless
the search produced coverage, jobs, and board-cache artifacts for that run.
The continuous service invokes the same script with `--search-only`, which
exits after publication and never enters document or application stages.

From Windows, trigger a reviewed out-of-cycle run with the confirmed absolute
POSIX clone path:

```powershell
pwsh scripts\trigger_vps_search.ps1 -RemoteRepoPath /absolute/path/to/Job-App-Automation
```

The trigger pulls output only after a successful remote run. A standalone pull
requires coverage, jobs, and board-cache files from the same remote commit and
updates the worktree without staging generated files:

```powershell
pwsh scripts\pull_search_output.ps1
pwsh scripts\check_sync_freshness.ps1
```

Private application reports are pulled separately over pinned SSH and never
through the generated-data branch:

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
pwsh scripts\check_vps_automation_status.ps1 -LogLines 120
```

This read-only command has a bounded SSH timeout and prints the installed cron
entry, remote clock/uptime, self-filtered automation processes, repository
commit/state, structured `output/vps_run_status.json`, key artifact timestamps
and sizes, and the requested tail of `output/vps_sync.log`. The status JSON is
updated atomically at every stage and on normal success or failure; a stale
`running` record after a reboot indicates an interrupted run.

Generated resume and cover-letter cleanup is dry-run by default:

```powershell
pwsh scripts\prune_old_outputs.ps1
```

Add `-Delete` only after reviewing the listed files. Keep
`config/vps_config.json` restricted and out of Git. The search trigger retains
its legacy password compatibility, while private document operations require
host-key pinning and support a dedicated archive key.
