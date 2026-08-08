# Job Application Automation

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/uv-supported-green.svg)](https://github.com/astral-sh/uv)
[![License](https://img.shields.io/badge/license-private-red.svg)]()

**Job Application Automation** is a local, safety-first Python engine for discovering public vacancies across 5 major ATS platforms (Greenhouse, Lever, Ashby, SmartRecruiters, Workable), generating AI-tailored PDF resumes & cover letters via Vertex AI, privately archiving documents on a VPS, and automating submissions with Playwright stealth browser automation. Includes Gmail OAuth integration and multi-candidate email rotation.

This README is the canonical documentation. See the [changelog](CHANGELOG.md)
for release history.

---

## Table of Contents

- [Capabilities](#capabilities)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Setup](#setup)
- [Configuration](#configuration)
- [Commands](#commands)
- [Safety & Privacy](#safety--privacy)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

---

## Capabilities

- **Job Discovery**: Search Greenhouse, Lever, Ashby, SmartRecruiters, Workable, and generic JSON-LD job pages without configured API credentials. Search produces CSV results, optional JSON, a reusable board cache, and a coverage report.
- **Tailored Resumes**: Generate role-specific PDF resumes with Vertex AI when configured, or a rule-based fallback if AI is unavailable. Source identity, employment facts, and education are validated before rendering.
- **Cover Letters**: Generate one-page, evidence-constrained cover letters with a JSON audit sidecar. The generator rejects invented source-claim IDs and invalid or multi-page output.
- **Document Archiving**: Generate matching CV/cover-letter pairs and store or retrieve them from private, hash-verified VPS storage using job URL, company, title, and candidate email.
- **Application Automation**: Apply to a single URL or an Excel tracker. The orchestrator detects the supported ATS, selects a configured candidate email, generates a tailored resume, and records results and confirmed submissions.
- **Submission Queue**: Run a deliberately live sequential queue that stops at the first unconfirmed application.
- **Gmail Integration**: Read, classify, redact, export, draft, or send Gmail messages through local OAuth, and select addresses from the configured candidate-email pool.
- **Status Dashboard**: Serve a minimal, read-only view of confirmed submissions, failures, backlog size, and worker state from local output artifacts.
- **Continuous Workers**: Run guarded, provider-specific Ashby, Greenhouse, and Lever workers with persistent state, pacing, cooldowns, and exact confirmation-ledger checks.
- **Google Indexing**: Submit sitemaps and eligible job pages to Google Search Console and the Indexing API.

---

## Quick Start

### First-Time Setup (5 minutes)

```powershell
# Clone and enter the repository
cd job-flow-ai

# Create virtual environment and install dependencies
uv sync --locked --no-dev

# Install Playwright Chromium
uv run playwright install chromium

# Review the five tracked files under config before live use.

# Create your resume source file
# Edit data\resumes\base-resume.txt with your actual resume content

# Verify setup
uv run python src/job_automation.py email-pool --count 1
```

### Common Workflows

#### 1. Search for Jobs
```powershell
uv run python src/job_automation.py search `
  --role-type "Product Manager" `
  --ats-platform greenhouse `
  --ats-platform lever `
  --location "Remote"
```

#### 2. Generate a Tailored Resume
```powershell
uv run python src/job_automation.py resume `
  --company "Example Corp" `
  --role "Senior Product Manager" `
  --url "https://jobs.ashbyhq.com/example/job-id"
```

#### 3. Generate a Cover Letter
```powershell
uv run python src/job_automation.py cover-letter `
  --company "Example Corp" `
  --role "Senior Product Manager" `
  --url "https://jobs.ashbyhq.com/example/job-id"
```

#### 4. Apply to a Job (Dry Run by Default)
```powershell
# Safe dry run - no submission
uv run python src/job_automation.py apply `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --dry-run

# Live submission (requires explicit flag)
uv run python src/job_automation.py apply `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --live-submit
```

---

## Project Structure

```
job-flow-ai/
├── src/
│   ├── job_automation.py              # CLI entry point
│   └── job_application_automation/    # Main package
│       ├── cli.py                     # Command dispatcher
│       ├── core/                      # Orchestration & workflows
│       ├── engines/                   # ATS provider adapters
│       ├── search/                    # Job discovery
│       ├── resume/                    # Resume & cover letter generation
│       ├── mail/                      # Gmail integration
│       └── dashboard/                 # HTTP dashboard server
├── config/                            # Configuration files
│   ├── runtime_config.json            # Operational defaults
│   └── candidate_profile_config.json  # Candidate data and policies
├── data/                              # Private local inputs and queues
│   ├── application-queues/            # Provider-specific retry/review JSON
│   ├── resumes/                       # Role-based PDF and text resumes
│   ├── templates/                     # Search and generation prompt templates
│   └── workbooks/                     # Private provider tracker workbooks
├── output/                            # Generated artifacts
├── pyproject.toml                     # Package metadata
└── README.md                          # This file
```

## Requirements

| Requirement | Purpose | Required For |
|-------------|---------|--------------|
| **Python 3.10+** | Runtime environment | All commands |
| **Chromium** | Browser automation | Application workflows |
| **Candidate config** | Profile, answers, policies | Resume & application workflows |
| **Resume source** | Base resume content (`data/resumes/base-resume.txt`) | Resume generation |
| **Google OAuth** | Gmail API access | Gmail commands |
| **Vertex AI** (optional) | AI-powered resume/cover letter | Falls back to rule-based if absent |
| **PuTTY tools** | VPS document archive | `documents store/retrieve` commands |

---

## Setup

### Standard Installation (pip)

```powershell
# Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
python -m pip install -r requirements.txt

# Install Playwright Chromium
python -m playwright install chromium
```

### Reproducible Installation (uv - Recommended)

```powershell
# Sync from lockfile (production)
uv sync --locked --no-dev
uv run playwright install chromium

# Sync with dev tools (contributors)
uv sync --locked --dev
```

### Install as Package

To use the `job-automation` command globally:

```powershell
python -m pip install .
job-automation --help

# Equivalent module entry point:
python -m job_application_automation --help
```

### Optional Extras

```powershell
# Structured Pydantic-based resume decoding
python -m pip install ".[structured]"

# Privacy-safe worker telemetry (Sentry)
python -m pip install ".[observability]"
```

> **Note:** All commands require `config/runtime_config.json` in the project where they run. The wheel no longer contains a second runtime-default copy.

---

## Configure local data and credentials

Tracked examples are safe templates. Copy and personalize them; the resulting files, credentials, tokens, candidate source text, and generated output are ignored by Git.

| Purpose | Local file | Starting point |
| --- | --- | --- |
| Candidate profile and answer policy | `config/candidate_profile_config.json` | Review before live use. |
| Candidate email addresses | `config/candidate_email_pool.json` | Review before live use. |
| Resume source material | `data/resumes/base-resume.txt` | Create from the candidate's resume; this is required for tailored resumes. |
| Runtime defaults | `config/runtime_config.json` | Single tracked operational configuration file. |
| Vertex service account | Path selected in `config/runtime_config.json` | Supply the credential outside Git. |
| Gmail desktop OAuth client and token | `config/credentials.json`, `config/token.json` | Download OAuth desktop-client credentials from Google Cloud; the token is created during authorization. |
| Private VPS archive | `config/vps_config.json` | Review the host-key fingerprint and archive authentication. |
| Google indexing | `config/seo_config.json` | Site/property data; pass private cloud credentials separately when using `google-indexing`. |

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
| `prefill-queue` | Fill a provider JSON queue locally | Background tabs; never submits; resumable |
| `gmail` | Read/export mail or create a draft/send a message | Draft/send only with `--send-to`; confirmation unless `--yes` |
| `email-pool` | Select configured candidate addresses | Local only |
| `dashboard` | Serve the read-only local status page | Starts an HTTP server; binds to `127.0.0.1:8765` by default |
| `google-indexing` | Submit the sitemap or eligible page notifications to Google | Live Google API calls unless `sitemap --dry-run` or `submit --dry-run` is used |
| `continuous-ashby` | Run the persistent guarded Ashby worker | Can submit jobs from its configured input |
| `continuous-greenhouse` | Run the persistent guarded Greenhouse worker | Can submit jobs from its configured input |
| `continuous-lever` | Run the persistent guarded Lever worker | Can submit jobs from its configured input |
| `engine <provider>` | Internal provider adapter used by `apply` | Orchestrator invocation required |

Compatibility aliases are `orchestrate` for `apply`, `archive` for `documents`, and `email` for `gmail`. Use `python src/job_automation.py <command> --help` for the complete, version-specific option list.

### Search job boards

`--role-type` and `--ats-platform` are required and repeatable. Supported platforms are `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workable`, and `web` (generic JSON-LD pages). Searches default to exhaustive board discovery, no rolling date limit, and the following locations when none is supplied: US Remote, UK, Ireland, India Remote, Delhi, Noida, France, Europe Remote, UAE, Saudi Arabia, Singapore, Australia, New Zealand, and Hong Kong.

```powershell
python src/job_automation.py search `
  --role-type "Product Manager" `
  --ats-platform greenhouse `
  --ats-platform lever `
  --ats-platform smartrecruiters `
  --ats-platform workable `
  --location "New York" `
  --verify-live `
  --require-live `
  --backlog-output output/job_backlog.json
```

Use `--posted-on YYYY-MM-DD`, or `--posted-since` with `--posted-until`, for explicit calendar filters. `--days 7` applies a rolling window. Seed known boards with `--board-url` or `--boards-file`, or scan company pages with `--career-page` or `--career-pages-file`. Results from the current run go to `output/ai_jobs.csv`; `output/job_search_coverage.json` explains discovery, feed, fallback, live-check, and backlog coverage. `--require-live` implies `--verify-live` and excludes unknown outcomes from the current-run view.

`--backlog-output output/job_backlog.json` enables the persistent active-job list used by the VPS. Each successful search merges newly found roles with the existing list, rechecks prior roles even when they were not rediscovered, and atomically rewrites the same file. Only exact `SUBMITTED & CONFIRMED` ledger evidence or a conclusive closed-role result removes a job. Failed/manual-review applications, timeouts, rate limits, server errors, and other unknown liveness outcomes remain in the backlog. No archive or tombstone file is created.

### Submit the site to Google

The general site is submitted through the Search Console Sitemaps API:

```powershell
# Validate the public/private config linkage without authenticating or mutating Google.
python src/job_automation.py google-indexing sitemap --dry-run

# Submit the configured sitemap to the configured Search Console domain property.
python src/job_automation.py google-indexing sitemap
```

Direct Indexing API notifications are restricted to same-domain pages with
`JobPosting` structured data or a qualifying `BroadcastEvent` nested in a
`VideoObject`. Update requests fetch the live page and verify this requirement;
delete requests require a `404`, `410`, or `noindex` response. General dashboard
pages belong in the sitemap and are rejected by the direct submission command.

```powershell
python src/job_automation.py google-indexing submit `
  --url "https://skybison.cloud/jobs/example" `
  --type URL_UPDATED `
  --dry-run

python src/job_automation.py google-indexing status `
  --url "https://skybison.cloud/jobs/example"
```

The service-account identity, key path, project, endpoint, scopes, and quotas
come from the private cloud configuration supplied to the command and its referenced key
file. Site ownership, sitemap URL, eligible URL list, timeout, and report path
come from `config/seo_config.json`. Every command atomically writes
`output/google_url_submission_report.json` unless `--report` overrides it.

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

Archives use opaque IDs, immutable manifests, pinned SSH host keys, private VPS permissions, and SHA-256 verification. They are stored outside the repository and never enter public generated output.

### Apply to jobs

Applications are dry runs unless `--live-submit` is explicitly passed. `--fill-only` drives the form without submitting. Review the generated result and visible browser state before any live action. Screenshots are isolated per attempt and deleted automatically when the application succeeds or fails.

```powershell
# One job; company and role are optional metadata in URL mode.
python src/job_automation.py apply `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --company "Example" `
  --role "Product Manager" `
  --dry-run

# A tracker-driven run. Default tracker and resume paths come from config/runtime_config.json.
python src/job_automation.py apply --limit 1 --dry-run
```

Use `--tracker`, `--resume`, `--config`, `--results-file`, and `--submission-log-file` to override defaults. `--headed` shows the browser, while `--no-shuffle` preserves tracker order. A URL run ignores `--tracker`. The orchestrator supports Ashby, Greenhouse, Lever, Workable, and SmartRecruiters and always enables URL-specific resume personalization. Company-board roots are rejected: every row must contain a job-specific URL. Workflow results are recorded in `output/orchestration_results.json`; only exact confirmed submissions are added to `output/submission_log.json`.

### Run a submission queue

The queue file contains one non-empty job URL per line. Unlike `apply`, queue execution always invokes `--live-submit`, and stops as soon as a submission is not confirmed.

```powershell
python src/job_automation.py queue --queue .\jobs.txt
```

Use `--start-index` to resume and `--timeout` to set each application-engine timeout. Progress is stored in `output/job_url_queue_progress.json`.

### Prefill a JSON queue locally

Use `prefill-queue` for a provider-specific JSON array when every application
must stop before submission. Run one process per ATS queue; each process uses
the configured Chrome debugging endpoint, creates one background tab per job,
generates a personalized resume and cover letter with one randomly selected
configured email, fills the form, and leaves the completed tab open.

```powershell
python src/job_automation.py prefill-queue `
  --queue data/application-queues/ashby-job-search-2026-08-04.json `
  --ats ashby
```

The command fails closed if the shared loopback Chrome CDP session is absent.
It disables live submission at the engine boundary, skips exact URLs found in
the confirmed submission ledger or its quarantine sidecar, and stores
resumable per-platform state under `output/local-prefill/`. A render hang gets
one reload in the same tab, followed by one retry in a replacement background
tab; only the stale tab owned by that job is closed. Generated state, logs, and
documents under `output/` are private and ignored by Git.

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

### Diagnostic application runs

Provider adapters are intentionally orchestrator-only. For an authorized diagnostic run, use `apply --url` so email selection, URL-specific documents, result persistence, and submission-log safeguards remain active. Use `--fill-only --headed` to inspect a form without submitting it.

```powershell
python src/job_automation.py apply --url "<authorized-url>" --fill-only --headed
```

The application pipeline can submit eligible
verified-live jobs whose document pair is already archived, including
Greenhouse, Lever, Ashby, SmartRecruiters, and Workable results. Document
failures remain visible without suppressing safe application work for other
archived jobs. The runner processes jobs sequentially, attempts no more than the
configured per-ATS limit, and skips every URL already present in confirmed or
attempted state. A CAPTCHA, missing required field, timeout, engine failure, or
unconfirmed attempt is recorded and skipped so processing can continue. Full
failure details are written privately to
`output/vps_application_failures.json` and per-job result files. Application
results, submission logs, and state remain private.
Application screenshots are temporary and are deleted at the end of every
successful, failed, timed-out, or quarantined attempt.

### View local status

Launch the embedded, read-only dashboard from the repository root:

```powershell
python src/job_automation.py dashboard
```

It reads current artifacts under `output/`, opens `http://127.0.0.1:8765` by
default, and refreshes every ten seconds. Use `--no-browser` for unattended
launches, or `--host` and `--port` to change the listener. The dashboard has no
authentication; do not expose it to an untrusted network.

### Run a continuous provider worker

The Ashby, Greenhouse, and Lever commands select verified-live jobs from their
configured input, prepare documents, invoke the guarded application workflow,
persist worker state, and enforce provider-specific pacing and cooldowns.
`--once` processes at most one job and is the recommended diagnostic mode:

```powershell
python src/job_automation.py continuous-greenhouse --once
```

Continuous workers are live application workflows, not dry runs. Review
`config/runtime_config.json`, the candidate profile, email pool, provider input,
and document/archive configuration before starting one. A worker attempt is not
a successful application unless the permanent ledger contains exact
`SUBMITTED & CONFIRMED` evidence.

### Outputs and exit status

| Path | Contents |
| --- | --- |
| `output/ai_jobs.csv` | Search results |
| `output/job_search_coverage.json` | Discovery, feed, fallback, and liveness coverage |
| `output/ats_boards_cache.json` | Reusable discovered-board cache |
| `output/job_backlog.json` | Persistent active, unsubmitted public job metadata; no archive |
| `output/orchestration_results.json` | Latest application workflow records |
| `output/submission_log.json` | Confirmed, non-test submissions only |
| `output/job_url_queue_progress.json` | Latest queue attempt and resume index |
| `output/local-prefill/<ats>/state.json` | Resumable local fill-only queue checkpoint |
| `output/application_documents/<id>/` | Locally generated archive-ready PDF pair and audit |
| `output/retrieved_documents/<id>/` | Verified PDFs and manifest retrieved from the VPS |

Commands generally return `0` for success, `1` for an unsuccessful workflow or remote operation, and `2` for invalid input. Gmail also returns `3` for an API error, `4` for dependency/authentication configuration errors, and `130` when interrupted. A zero exit status from a dry run or fill-only run is not evidence of submission; only an exact, non-test `SUBMITTED & CONFIRMED` result is recorded as complete.

## Safety & Privacy

**Critical Guidelines:**

- ✅ **Only automate with explicit authorization** for the candidate account and target job
- ✅ **Keep credentials out of version control**: OAuth tokens, service accounts, SSH keys, passwords
- ✅ **Use private VPS archive** outside repository and web roots
- ✅ **Verify confirmation status** before treating an application as submitted
- ✅ **Use queue command only for intentional live submissions** (not dry-run)

**What Never Gets Committed:**
- Credentials, tokens, or API keys
- Personal candidate data (profile, resume source)
- Generated application artifacts
- Unredacted screenshots
- Private archive contents

---

## Architecture

```
job_automation.py
  └─ cli.py
      ├─ apply / queue              core orchestration and shared contracts
      ├─ resume                     resume source, AI, validation, scoring, rendering
      ├─ cover-letter               evidence-constrained generation and one-page validation
      ├─ documents                  paired generation and private VPS archive
      ├─ search                     discovery, board feeds, JSON-LD, liveness, caching
      ├─ gmail / email-pool         Gmail OAuth, messages, exports, email selection
      ├─ dashboard                  embedded read-only status page and aggregate API
      ├─ continuous-*               guarded provider workers and persistent state
      └─ engine <ATS>               guarded provider-specific browser engine
```

### Key Components

| Component | Responsibility |
|-----------|----------------|
| `core/` | Orchestration, workflows, state management |
| `engines/` | ATS provider adapters (Ashby, Greenhouse, Lever, etc.) |
| `search/` | Job discovery across multiple providers |
| `resume/` | Resume and cover letter generation |
| `mail/` | Gmail OAuth integration |
| `dashboard/` | HTTP dashboard server (read-only, unauthenticated) |

---

## Troubleshooting

Common issues and solutions:

| Issue | Solution |
|-------|----------|
| Playwright browser errors | Run `playwright install chromium` |
| Missing config files | Restore or review the five tracked files under `config/` |
| OAuth authentication failures | Re-authorize using Gmail flow; check `credentials.json` |
| VPS connection refused | Verify SSH host key fingerprint in `vps_config.json` |
| Application timeouts | Increase `--timeout` flag or check network stability |
| Resume generation fails | Ensure `data/resumes/base-resume.txt` exists with valid content |

---

## Contributing

### Quick Start for Contributors

```powershell
# Clone and setup
uv sync --locked --dev
uv run playwright install chromium

# Run quality checks before committing
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run compileall -q src
uv run pip check
```

### Change Boundaries

| Directory | Purpose |
|-----------|---------|
| `engines/` | Provider-specific browser behavior |
| `core/` | Workflow coordination |
| `resume/` | Resume and cover letter concerns |
| `core/artifacts` | Persisted files |

### Pull Request Guidelines

When submitting PRs, include:
- User-visible behavior changes
- Safety impact assessment
- Validation performed
- README updates when CLI, configuration, or provider behavior changes

---

## License & Acknowledgments

This project is a private toolkit for authorized job application automation. Use responsibly and only with explicit permission from candidates whose profiles and accounts are managed through this system.

---

**Last Updated:** August 8, 2026 · **Version:** 0.1.0 (pre-release)
