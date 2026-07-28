# Job Application Automation

Tools for searching ATS job boards, creating personalized resumes, and
automating Ashby, Greenhouse, and Lever application forms.

## Layout

```text
src/       Python scripts
config/    Candidate settings and local OAuth files
data/      Base resume material and the job tracker
output/    Generated resumes, caches, and run results
```

The project intentionally uses only one folder level below the repository root.

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
python src/orchestrator.py --dry-run --limit 1
```

Run the complete workflow for one job URL:

```powershell
python src/orchestrator.py `
  --url "https://jobs.ashbyhq.com/example/job-id" `
  --dry-run
```

Job URLs must enter through `orchestrator.py`; the ATS engine scripts
(`engine_ashby.py`, `engine_greenhouse.py`, `engine_lever.py`) are internal
child processes and reject direct URL invocations.

Generate a personalized resume:

```powershell
python src/resume_generate.py `
  --company "Example" `
  --role "Product Manager" `
  --url "https://jobs.example.com/role"
```

Search supported ATS boards:

```powershell
python src/search_job_boards.py `
  --role-type "Product Manager" `
  --ats-platform greenhouse `
  --ats-platform lever `
  --location "New York" `
  --verify-live `
  --require-live
```

`--role-type`, `--ats-platform`, and `--location` are required. Repeat
`--role-type`, `--ats-platform`, or `--location` to search multiple role
families, platforms, or locations. Expanded role matching has built-in support
for Growth Marketing (`Growth Mkt`), Performance Marketing (`Performance Mkt`),
Paid Media, Marketing Operations (`Marketing Ops`), Management Consulting,
Corporate Development (`Corp Dev`), and Venture Capital. For example:

```powershell
python src/search_job_boards.py `
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
python src/email_gmail_client.py --max-results 10
```

Generated resumes, caches, screenshots, and run results are written to
`output/`.

## Safety

Application commands default to dry-run behavior. Review generated answers and
use the explicit live-submit option only when you intend to submit an
application.
