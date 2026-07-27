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
python src/search_job_boards.py --help
```

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
