# Job Application Automation

Tools for searching ATS job boards, creating personalized resumes, and
automating Ashby, Greenhouse, and Lever application forms.

## Layout

```text
src/job_automation.py              Unified command launcher
src/job_application_automation/    Reusable workflow implementation package
config/                             Candidate settings and local OAuth files
data/                               Base resume material and the job tracker
output/                             Generated resumes, caches, and run results
```

Run workflows through `src/job_automation.py`; the implementation package is
not a collection of independently executed scripts.

### Command migration

The previous root-level workflow scripts have been removed. Replace existing
commands with the corresponding unified subcommand:

| Previous command | Replacement |
| --- | --- |
| `python src/orchestrator.py ...` | `python src/job_automation.py apply ...` |
| `python src/queue_runner.py ...` | `python src/job_automation.py queue ...` |
| `python src/resume_generate.py ...` | `python src/job_automation.py resume ...` |
| `python src/search_job_boards.py ...` | `python src/job_automation.py search ...` |
| `python src/email_gmail_client.py ...` | `python src/job_automation.py gmail ...` |
| `python src/email_pool_select.py ...` | `python src/job_automation.py email-pool ...` |

Code that imported a root facade should import its module from
`job_application_automation` instead, for example
`from job_application_automation import orchestrator`.

## Setup

Create a virtual environment and install the dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

The resume generator uses the Google GenAI SDK with Vertex AI. Configure
Application Default Credentials or a service-account credential through the
standard `GOOGLE_APPLICATION_CREDENTIALS` environment variable. It can fall
back to rule-based local generation when the AI service is unavailable.

## Development quality checks

Install the runtime and development dependencies, then run the same local,
non-submitting checks used by CI:

```powershell
python -m pip install -r requirements-dev.txt
python -m ruff format --check .
python -m ruff check .
python -m pytest
python -m compileall -q src
python -m pip check
```

Pytest is configured to discover tests in `tests/`, import package modules from
`src/`, and report branch coverage for that source directory.
CI disables sockets for the test run, so automated checks must use mocks or
fixtures for external services. The workflow invokes no live ATS, Gmail,
browser, or LLM operation.

## Configuration

- Candidate details: `config/candidate_profile_config.json`
- Candidate email pool: `config/candidate_email_pool.json`
- Gmail desktop OAuth client: `config/credentials.json`
- Gmail OAuth token: `config/token.json`
- Job tracker: `data/ai_product_manager_job_tracker.xlsx`
- Resume source text: `data/base_resume.txt`
- Base resume PDF: `data/shivam_singh_ai_product_manager_resume.pdf`

OAuth files are deliberately excluded from Git.

## Common commands

Preview the orchestrator without submitting applications:

```powershell
python src/job_automation.py apply --dry-run --limit 1
```

Run the complete workflow for one job URL:

```powershell
python src/job_automation.py apply `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --dry-run
```

Job URLs must enter through `job_automation.py apply`. The ATS engines are
internal package workflows invoked only by that command.

Generate a personalized resume:

```powershell
python src/job_automation.py resume `
  --company "Example" `
  --role "Product Manager" `
  --url "https://jobs.example.com/role"
```

Search supported ATS boards:

```powershell
python src/job_automation.py search `
  --role-type "Product Manager" `
  --ats-platform greenhouse `
  --ats-platform lever `
  --location "New York" `
  --verify-live `
  --require-live
```

`--role-type` and `--ats-platform` are required. Repeat `--role-type`,
`--ats-platform`, or `--location` to search multiple role families, platforms,
or locations. When `--location` is omitted, the search defaults to US Remote,
UK, Ireland, India Remote, Delhi, Noida, France, Europe Remote, UAE, Saudi
Arabia, Singapore, Australia, New Zealand, and Hong Kong. Supplying one or
more `--location` values replaces that default list. Expanded role matching has
built-in support for Growth Marketing (`Growth Mkt`), Performance Marketing
(`Performance Mkt`), Paid Media, Marketing Operations (`Marketing Ops`),
Management Consulting, Corporate Development (`Corp Dev`), and Venture Capital.
For example:

```powershell
python src/job_automation.py search `
  --role-type "Growth Mkt" `
  --role-type "Paid Media" `
  --role-type "Corp Dev" `
  --ats-platform greenhouse `
  --ats-platform lever `
  --location "New York"
```

The `web` platform adds JSON-LD job-page discovery for common public ATSs that
do not have a stable board-feed adapter. The final match uses all configured
title variants, while discovery uses one canonical phrase per requested family
so a query budget cannot be consumed by abbreviations or input typos.

Use `--posted-on 2026-07-28` for one calendar day, or combine
`--posted-since` and `--posted-until` for a date range. Explicit posted-date
filters take precedence over the default unbounded `--days 0` window.

Searches run in exhaustive discovery mode by default: they use broad role,
AI, location, and career queries to find boards, then apply the requested
filters only after reading the live board feed or job page. Results include a
coverage report at `output/job_search_coverage.json`. Use `--days 7` for a
recent-only run, `--career-page https://example.com/careers` to scan a custom
career page for ATS links, and `--boards-file boards.txt` to reuse a maintained
list of known boards. The default 400-request discovery budget prioritizes an
early query wave across requested role families; use
`--max-discovery-queries 0` when you explicitly want every planned query to
run.

Unverified feed results are marked `listed`. `--verify-live` records a
`live`, `closed`, or `unknown` outcome when it has decisive evidence.
`--require-live` keeps only roles confirmed live; transient access failures
remain `unknown` rather than being treated as closed. A provider-confirmed role
can remain `listed` when its page blocks verification or has ambiguous evidence;
`--require-live` excludes it. Generic JSON-LD roles are page-verified because
their identifiers are not assumed to be provider API IDs.

Read recent Gmail messages:

```powershell
python src/job_automation.py gmail --max-results 10
```

Generated resumes, caches, screenshots, and run results are written to
`output/`.

## Safety

Application commands default to dry-run behavior. Review generated answers and
use the explicit live-submit option only when you intend to submit an
application.

## Manual credentialed smoke checklist

Run this only when you have explicit authorization for the candidate profile,
OAuth account, and target ATS job. These steps are intentionally manual and are
not part of CI.

1. Create the local candidate configuration from the example files, provide
   the required Gmail OAuth and Vertex AI credentials, and install Chromium
   with `python -m playwright install chromium`. Keep all credentials out of
   Git.
2. Use a test candidate and an Ashby, Greenhouse, or Lever URL you are
   authorized to exercise. Start with `python src/job_automation.py apply --url
   "<authorized-supported-ats-url>" --dry-run` and inspect the generated
   result, resume, and any screenshots.
3. If an authorized test application needs browser form coverage, rerun the
   same command with `--fill-only`; review every filled response before taking
   any further action.
4. Only after deliberate human review, use `--live-submit` for an application
   you intend to submit. Confirm the recorded result before moving to another
   job.
