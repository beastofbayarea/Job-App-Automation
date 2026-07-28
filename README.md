# Job Application Automation

A local, safety-first toolkit for discovering public ATS vacancies, generating tailored PDF resumes and cover letters, privately archiving those documents on a VPS, and filling applications on Ashby, Greenhouse, and Lever. It also includes a Gmail OAuth utility and a candidate-email pool selector.

The public entry point is `src/job_automation.py`. The implementation under `src/job_application_automation/` is a reusable Python package; ATS engine commands are primarily diagnostics used by the orchestrator.

## Capabilities

- Search Greenhouse, Lever, Ashby, and generic JSON-LD job pages without API keys. Search produces CSV results, optional JSON, a reusable board cache, and a coverage report.
- Generate a role-specific PDF resume with Vertex AI when configured, or a rule-based fallback if AI is unavailable. Source identity, employment facts, and education are validated before rendering.
- Generate a matching CV/cover-letter pair and store or retrieve it from private, hash-verified VPS storage using the job URL, company, title, and candidate email.
- Apply to a single URL or an Excel tracker. The orchestrator detects the supported ATS, selects a configured candidate email, generates a tailored resume, and records results and confirmed submissions.
- Run a deliberately live sequential queue that stops at the first unconfirmed application.
- Read, classify, export, draft, or send Gmail messages through local OAuth.

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

Run all workflows from the repository root.

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

# Create a draft. Omit --yes to require an interactive confirmation.
python src/job_automation.py gmail --send-to "person@example.com" --subject "Hello" --body "Message" --draft

# Select a configured candidate email.
python src/job_automation.py email-pool --count 1
```

Gmail supports `--all-mail`, `--unread`, `--json`, HTML bodies, drafts, and sending. It requires local OAuth credentials and does not run in CI.

### Diagnostic engine commands

The engines are normally started by `apply`. For an authorized diagnostic run, use `engine ashby`, `engine greenhouse`, or `engine lever`; each requires `--url` and `--resume` and accepts the same `--dry-run`, `--fill-only`, `--live-submit`, and `--headed` controls.

```powershell
python src/job_automation.py engine greenhouse --url "<authorized-url>" --resume ".\resume.pdf" --dry-run
```

## Architecture

```text
job_automation.py
  └─ cli.py
      ├─ apply / queue              core orchestration and shared contracts
      ├─ resume                     resume source, AI, validation, scoring, rendering
      ├─ documents                  paired generation and private VPS archive
      ├─ search                     discovery, board feeds, JSON-LD, liveness, caching
      ├─ gmail / email-pool         Gmail OAuth, messages, exports, email selection
      └─ engine <ATS>               Ashby, Greenhouse, or Lever browser engine
```

See [architecture.mmd](architecture.mmd) for the detailed Mermaid diagram. `PRD.md` and `docs/superpowers/plans/` describe planned work, not currently available commands.

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
