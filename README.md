# Job Application Automation

A local, safety-first toolkit for discovering public ATS vacancies, generating tailored PDF resumes and cover letters, privately archiving those documents on a VPS, and filling applications on Ashby, Greenhouse, and Lever. It also includes a Gmail OAuth utility and a candidate-email pool selector.

The public entry point is `src/job_automation.py`. The implementation under `src/job_application_automation/` is a reusable Python package; ATS engine commands are primarily diagnostics used by the orchestrator.

## Capabilities

- Search Greenhouse, Lever, Ashby, and generic JSON-LD job pages without API keys. Search produces CSV results, optional JSON, a reusable board cache, and a coverage report.
- Generate a role-specific PDF resume with Vertex AI when configured, or a rule-based fallback if AI is unavailable. Source identity, employment facts, and education are validated before rendering.
- Generate a one-page, evidence-constrained cover letter with a JSON audit sidecar. The generator rejects invented source-claim IDs and invalid or multi-page output.
- Generate a matching CV/cover-letter pair and store or retrieve it from private, hash-verified VPS storage using the job URL, company, title, and candidate email.
- Apply to a single URL or an Excel tracker. The orchestrator detects the supported ATS, selects a configured candidate email, generates a tailored resume, and records results and confirmed submissions.
- Run a deliberately live sequential queue that stops at the first unconfirmed application.
- Read, classify, redact, export, draft, or send Gmail messages through local OAuth, and select addresses from the configured candidate-email pool.
- Run scheduled searches and guarded automatic applications on a VPS, synchronize
  public-safe results, verify freshness, rotate logs, and prune old generated PDFs.

## Requirements

- Python 3.10 or newer
- Chromium for browser-based application flows
- Candidate configuration and resume source material for resume or application workflows
- Google OAuth credentials for Gmail features
- A Google Cloud Vertex AI credential is optional; without it, resume generation falls back locally
- PuTTY `plink` and `pscp` for private VPS document storage and retrieval

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

To install the reusable package and the `job-automation` command, use:

```powershell
python -m pip install .
job-automation --help
# Equivalent module entry point:
python -m job_application_automation --help
```

Installed commands look for local candidate files and an optional
`config/runtime_config.json` in the directory where they are run. When that
file is absent, they use the package's safe default operational settings; copy
the tracked runtime config to customize paths or timeouts.

For contributor checks, install the development dependencies as well:

```powershell
python -m pip install -r requirements-dev.txt
```

## Configure local data and credentials

Tracked examples are safe templates. Copy and personalize them; the resulting files, credentials, tokens, candidate source text, and generated output are ignored by Git.

| Purpose | Local file | Starting point |
| --- | --- | --- |
| Candidate profile and answer policy | `config/candidate_profile_config.json` | `config/candidate_profile_config.example.json` |
| Candidate email addresses | `config/candidate_email_pool.json` | `config/candidate_email_pool.example.json` |
| Resume source material | `data/base_resume.txt` | Create from the candidate's resume; this is required for tailored resumes. |
| Runtime defaults | `config/runtime_config.json` | Already tracked; adjust paths, timeouts, model, and quality thresholds when needed. |
| Vertex service account | `config/vertex_service_account.json` | `config/vertex_service_account.example.json` |
| Gmail desktop OAuth client and token | `config/credentials.json`, `config/token.json` | Download OAuth desktop-client credentials from Google Cloud; the token is created during authorization. |
| Private VPS archive | `config/vps_config.json` | Copy `config/vps_config.example.json`, then add a trusted host-key fingerprint and dedicated archive authentication. |

The default runtime configuration resolves paths from the project root. Its Vertex `project_id` can remain `from-service-account` to read the project ID from the configured service-account file. Alternatively, use Application Default Credentials via `GOOGLE_APPLICATION_CREDENTIALS`.

## Commands

Run all workflows from the repository root. These forms are equivalent after installing the package:

```powershell
python src/job_automation.py <command>
python -m job_application_automation <command>
job-automation <command>
```

| Command | Function | Live action boundary |
| --- | --- | --- |
| `search` | Discover and filter public ATS jobs | Public HTTP/search requests |
| `resume` | Generate one tailored resume PDF | May call Vertex AI |
| `cover-letter` | Generate one validated one-page cover letter and audit JSON | May call Vertex AI |
| `documents generate` | Generate a matched resume and cover letter | VPS upload only with `--archive` |
| `documents store` | Validate/hash an existing PDF pair | VPS upload only with `--execute` |
| `documents retrieve` | Download and verify an archived PDF pair | Always contacts the configured VPS |
| `apply` | Run one URL or tracker-driven ATS workflow | Submission only with `--live-submit` |
| `queue` | Run a sequential URL queue | Always requests live submission |
| `gmail` | Read/export mail or create a draft/send a message | Draft/send only with `--send-to`; confirmation unless `--yes` |
| `email-pool` | Select configured candidate addresses | Local only |
| `engine <provider>` | Direct Ashby, Greenhouse, or Lever diagnostic | Submission only with `--live-submit` |

Compatibility aliases are `orchestrate` for `apply`, `archive` for `documents`, and `email` for `gmail`. Use `python src/job_automation.py <command> --help` for the complete, version-specific option list.

### Search job boards

`--role-type` and `--ats-platform` are required and repeatable. Supported platforms are `greenhouse`, `lever`, `ashby`, and `web` (generic JSON-LD pages). Searches default to exhaustive board discovery, no rolling date limit, and the following locations when none is supplied: US Remote, UK, Ireland, India Remote, Delhi, Noida, France, Europe Remote, UAE, Saudi Arabia, Singapore, Australia, New Zealand, and Hong Kong.

```powershell
python src/job_automation.py search `
  --role-type "Product Manager" `
  --ats-platform greenhouse `
  --ats-platform lever `
  --location "New York" `
  --verify-live `
  --require-live
```

Use `--posted-on YYYY-MM-DD`, or `--posted-since` with `--posted-until`, for explicit calendar filters. `--days 7` applies a rolling window. Seed known boards with `--board-url` or `--boards-file`, or scan company pages with `--career-page` or `--career-pages-file`. Results go to `output/ai_jobs.csv`; `output/job_search_coverage.json` explains discovery, feed, fallback, and live-check coverage. `--require-live` implies `--verify-live` and excludes unknown outcomes.

### Generate a resume

```powershell
python src/job_automation.py resume `
  --company "Example" `
  --role "Product Manager" `
  --url "https://jobs.ashbyhq.com/example/job-id"
```

Provide job-description text directly with `--keywords`, `--jd-overview`, `--jd-resp`, and `--jd-req`; otherwise an Ashby URL is used for context. Use `--email` to override the source email for one generated document and `--output` to choose its path. Generated resumes and non-persistent caches are written under `output/` by default.

### Generate a cover letter

The standalone workflow requires job-description context from segmented text, `--jd-file`, or an Ashby `--url` fallback. It produces a one-page PDF and a sibling `.audit.json` file that records evidence claim IDs and source hashes.

```powershell
python src/job_automation.py cover-letter `
  --company "Example" `
  --role "Product Manager" `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --jd-file ".\job-description.txt" `
  --email "candidate@example.com" `
  --output ".\output\Example_Product_Manager_Cover_Letter.pdf"
```

`--profile` overrides the candidate profile. The email override affects only this generated document. If both `--jd-file` and segmented `--jd-*` values are provided, the file is authoritative.

### Generate and privately archive a CV and cover letter

The `documents` workflow gives the resume and cover letter one shared identity. Local generation does not contact the VPS:

```powershell
python src/job_automation.py documents generate `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --company "Example" `
  --job-title "Product Manager" `
  --email "candidate@example.com" `
  --jd-file ".\job-description.txt"
```

For an intentional one-step generate-and-upload run, add `--archive`. To archive the exact files after reviewing them, use `documents store` instead.

Existing PDFs can be validated without network access, then explicitly uploaded:

```powershell
# Local plan only.
python src/job_automation.py documents store `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --company "Example" `
  --job-title "Product Manager" `
  --email "candidate@example.com" `
  --cv ".\output\resume.pdf" `
  --cover-letter ".\output\cover_letter.pdf"

# Add --execute to perform the upload.
```

Retrieve both PDFs with the same four selectors:

```powershell
python src/job_automation.py documents retrieve `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --company "Example" `
  --job-title "Product Manager" `
  --email "candidate@example.com"
```

Archives use opaque IDs, immutable manifests, pinned SSH host keys, private VPS permissions, and SHA-256 verification. They are stored outside the repository and never enter `vps-search-output`. See the [operations runbook](docs/operations-runbook.md) for one-time VPS setup.

### Apply to jobs

Applications are dry runs unless `--live-submit` is explicitly passed. `--fill-only` drives the form without submitting. Review generated results and screenshots before any live action.

```powershell
# One job; company and role are optional metadata in URL mode.
python src/job_automation.py apply `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --company "Example" `
  --role "Product Manager" `
  --dry-run

# A tracker-driven run. The default tracker and resume paths come from runtime_config.json.
python src/job_automation.py apply --limit 1 --dry-run
```

Use `--tracker`, `--resume`, `--config`, `--results-file`, and `--submission-log-file` to override defaults. `--headed` shows the browser, while `--no-shuffle` preserves tracker order. A URL run ignores `--tracker`. The orchestrator supports Ashby, Greenhouse, and Lever and always enables URL-specific resume personalization. It records workflow results in `output/orchestration_results.json` and confirmed applications in `output/submission_log.json` by default.

### Run a submission queue

The queue file contains one non-empty job URL per line. Unlike `apply`, queue execution always invokes `--live-submit`, and stops as soon as a submission is not confirmed.

```powershell
python src/job_automation.py queue --queue .\jobs.txt
```

Use `--start-index` to resume and `--timeout` to set each application-engine timeout. Progress is stored in `output/job_url_queue_progress.json`.

### Gmail and email pool

```powershell
# Read and classify recent matching messages.
python src/job_automation.py gmail --query "newer_than:30d" --classify --include-body

# Export messages without exposing sensitive data in the export.
python src/job_automation.py gmail --unread --csv output\mail.csv --redact

# Create a draft after interactive confirmation.
python src/job_automation.py gmail --send-to "person@example.com" --subject "Hello" --body "Message" --draft

# Send after interactive confirmation. Add --yes only for an intentional,
# non-interactive draft or send.
python src/job_automation.py gmail --send-to "person@example.com" --subject "Hello" --body "Message"

# Select a configured candidate email.
python src/job_automation.py email-pool --count 1
```

Gmail supports `--max-results`, `--all-mail`, `--unread`, `--query`, `--include-body`, `--classify`, CSV/JSON exports, `--redact`, plain-text or HTML bodies, drafts, and sending. Reading is the default when `--send-to` is absent. It requires local OAuth credentials and does not run in CI. The email-pool command randomly selects `--count` addresses and accepts a custom pool through `--file`.

### Diagnostic engine commands

The engines are normally started by `apply`. For an authorized diagnostic run, use `engine ashby`, `engine greenhouse`, or `engine lever`; each requires `--url` and `--resume` and accepts the same `--dry-run`, `--fill-only`, `--live-submit`, and `--headed` controls.

```powershell
python src/job_automation.py engine greenhouse --url "<authorized-url>" --resume ".\resume.pdf" --dry-run
```

### VPS synchronization and maintenance scripts

These are operational helpers rather than `job-automation` subcommands:

```powershell
# Pull one complete generated-output snapshot from origin/vps-search-output.
pwsh scripts\pull_search_output.ps1

# Privately download the confirmed-submission list and latest failure report.
pwsh scripts\pull_vps_application_reports.ps1

# Inspect the active VPS process and recent log without starting a run.
pwsh scripts\check_vps_automation_status.ps1

# Check output/job_search_coverage.json age (24 hours by default).
pwsh scripts\check_sync_freshness.ps1 -ThresholdHours 24

# Trigger an out-of-cycle search on the VPS, then pull it on success.
pwsh scripts\trigger_vps_search.ps1 `
  -RemoteRepoPath /absolute/path/to/Job-App-Automation

# Preview old top-level generated resume/cover-letter PDFs; deletion is explicit.
pwsh scripts\prune_old_outputs.ps1 -Days 14
pwsh scripts\prune_old_outputs.ps1 -Days 14 -Delete
```

On the VPS, `scripts/vps_search_sync.sh` runs the configured search under a nonblocking lock and publishes a complete result set to the dedicated `vps-search-output` branch. Install repository-aware log rotation with `bash scripts/install_vps_logrotate.sh`; add `--stdout` to preview the rendered policy. These workflows, their prerequisites, cron guidance, and safe branch boundary are detailed in the [operations runbook](docs/operations-runbook.md).

Install or repair the unattended daily schedule and private archive root from
Windows with:

```powershell
pwsh scripts\install_vps_daily_automation.ps1 `
  -RemoteRepoPath /absolute/path/to/Job-App-Automation
```

The installer pins the configured SSH host key, replaces only its own marked
cron entry, installs log rotation, and creates the private archive directory
with mode `0700`.

Install either or both supervised one-job ATS workers:

```powershell
pwsh scripts\install_vps_continuous_ashby.ps1
# Or, when Greenhouse is the intended provider:
pwsh scripts\install_vps_continuous_greenhouse.ps1
```

The installers deploy the ignored candidate email pool, remove the older daily
cron entry, disable the broad continuous search/submission service, and enable
`job-app-ashby.service` and/or `job-app-greenhouse.service`. The provider
workers may run in parallel: each uses its own job list, state, documents, and
results while cross-process locking protects the shared confirmation ledger.
Each cycle randomly selects one fresh, verified-live role for that ATS,
chooses one configured candidate email, generates a matching personalized
resume and cover letter, passes both files through the guarded orchestrator,
and accepts only exact
`SUBMITTED & CONFIRMED` ledger evidence. It then waits a random 120-300 seconds
before the next cycle. Systemd restarts the worker after crashes and boots.
Ambiguous or interrupted submission attempts are quarantined for review and
are never retried automatically.

Each successful scheduled search publishes its complete safe snapshot before
starting private document work. The workflow then processes a bounded number of
tailored CV/cover-letter pairs per run (10 by default, including two reserved
retry slots) and uploads successful pairs to the private VPS archive. An
ignored state file records completed URLs, so later runs skip archived pairs
while continuing through both new jobs and prior failures. Full job
descriptions and document-generation state remain private on the VPS and are
never copied to `vps-search-output`.

The VPS then submits only eligible verified-live Greenhouse, Lever, and Ashby
jobs whose document pair is already archived. Document failures remain visible
without suppressing safe application work for other archived jobs. The runner
processes jobs sequentially, attempts no more than the configured per-ATS limit,
and skips every URL already present in confirmed or attempted state. A CAPTCHA,
missing required field, timeout, engine failure, or unconfirmed attempt is
recorded and skipped so processing can continue. Full failure details are
written privately to
`output/vps_application_failures.json` and per-job result files. Application
results, screenshots, submission logs, and state never enter
`vps-search-output`.

Use `scripts/pull_vps_application_reports.ps1` whenever the private confirmed
submission list and latest failure report are needed locally. It downloads both
files through pinned SSH into `output/vps_reports/`, validates both JSON files,
and leaves existing local reports untouched unless `-Overwrite` is explicit.

### Outputs and exit status

| Path | Contents |
| --- | --- |
| `output/ai_jobs.csv` | Search results |
| `output/job_search_coverage.json` | Discovery, feed, fallback, and liveness coverage |
| `output/ats_boards_cache.json` | Reusable discovered-board cache |
| `output/orchestration_results.json` | Latest application workflow records |
| `output/submission_log.json` | Confirmed, non-test submissions only |
| `output/job_url_queue_progress.json` | Latest queue attempt and resume index |
| `output/application_documents/<id>/` | Locally generated archive-ready PDF pair and audit |
| `output/retrieved_documents/<id>/` | Verified PDFs and manifest retrieved from the VPS |

Commands generally return `0` for success, `1` for an unsuccessful workflow or remote operation, and `2` for invalid input. Gmail also returns `3` for an API error, `4` for dependency/authentication configuration errors, and `130` when interrupted. A zero exit status from a dry run or fill-only run is not evidence of submission; only an exact, non-test `SUBMITTED & CONFIRMED` result is recorded as complete.

## Architecture

```text
job_automation.py
  └─ cli.py
      ├─ apply / queue              core orchestration and shared contracts
      ├─ resume                     resume source, AI, validation, scoring, rendering
      ├─ cover-letter               evidence-constrained generation and one-page validation
      ├─ documents                  paired generation and private VPS archive
      ├─ search                     discovery, board feeds, JSON-LD, liveness, caching
      ├─ gmail / email-pool         Gmail OAuth, messages, exports, email selection
      └─ engine <ATS>               Ashby, Greenhouse, or Lever browser engine
```

See [architecture.mmd](architecture.mmd) for the detailed Mermaid diagram. `PRD.md` is a living roadmap with explicit implementation status. Files under `docs/superpowers/` are historical design and implementation records, not current command references.

## Documentation

The [documentation guide](docs/README.md) links to detailed configuration, CLI, operations, data-format, ATS-support, security, architecture, and troubleshooting references. Contributors should read [CONTRIBUTING.md](CONTRIBUTING.md); user-facing changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## Quality checks

```powershell
python -m ruff format --check .
python -m ruff check .
python -m pytest
python -m compileall -q src
python -m pip check
```

Pytest runs with sockets disabled, so tests use mocks and never invoke live ATS, browser, Gmail, or LLM operations.

## Safety and privacy

- Only use browser automation and email features with explicit authorization for the candidate account and target job.
- Keep credentials, OAuth tokens, candidate profile data, resume source material, and generated output out of version control.
- Keep the private archive outside the VPS repository and web roots; never publish its PDFs, manifest, or email metadata through Git.
- Do not treat a filled form as submitted: rely on the recorded confirmation status before proceeding.
- Use the queue only for intentional live submissions; it is not a dry-run batch tool.
