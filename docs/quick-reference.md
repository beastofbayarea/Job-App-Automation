# Quick Reference & Cheat Sheet

This is a concise reference for common operations. For detailed guidance, see the full documentation.

##  Installation

```powershell
# Recommended (uv)
uv sync --locked --no-dev
uv run playwright install chromium

# Alternative (pip)
python -m venv .venv
python -m pip install -r requirements.txt
python -m playwright install chromium

# Install as package
python -m pip install .
```

##  Setup Checklist

```powershell
# 1. Copy configuration templates
# Review config/candidate_profile_config.json and config/candidate_email_pool.json

# 2. Create base resume
echo "Your resume content" > data\resumes\base-resume.txt

# 3. Verify setup
uv run python src/job_automation.py email-pool --count 1
```

##  Commands Overview

| Command | Purpose | Key Flags |
|---------|---------|-----------|
| `search` | Find jobs | `--role-type`, `--ats-platform`, `--location` |
| `resume` | Generate resume | `--company`, `--role`, `--url` |
| `cover-letter` | Generate cover letter | `--company`, `--role`, `--url` |
| `documents generate` | Generate both | `--url`, `--company`, `--job-title`, `--email` |
| `documents store` | Archive to VPS | `--url`, `--company`, `--job-title`, `--email`, `--cv`, `--cover-letter` |
| `documents retrieve` | Get from VPS | `--url`, `--company`, `--job-title`, `--email` |
| `apply` | Apply to job | `--url`, `--dry-run`, `--fill-only`, `--live-submit` |
| `queue` | Batch apply | `--queue`, `--start-index` |
| `gmail` | Gmail ops | `--query`, `--send-to`, `--draft`, `--redact` |
| `email-pool` | Select email | `--count`, `--file` |
| `google-indexing` | Google submission | `sitemap`, `submit`, `status` |

##  Common Workflows

### 1. Find Jobs

```powershell
# Basic search
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse lever

# With location filter
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --location "Remote" \
  --location "New York"

# Verify jobs are live
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --verify-live --require-live

# Date filtering
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --posted-since 2025-01-01 \
  --posted-until 2025-08-01

# Rolling window (last 7 days)
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --days 7
```

### 2. Generate Documents

```powershell
# Resume from URL
uv run python src/job_automation.py resume \
  --company "Example Corp" \
  --role "Senior Product Manager" \
  --url "https://jobs.ashbyhq.com/example/job-id"

# Resume with job description text
uv run python src/job_automation.py resume \
  --company "Example Corp" \
  --role "Senior Product Manager" \
  --keywords "product,management,agile" \
  --jd-overview "Build and scale products..." \
  --jd-resp "Define roadmap, work with engineers..." \
  --jd-req "5+ years experience, product sense..."

# Cover letter from URL
uv run python src/job_automation.py cover-letter \
  --company "Example Corp" \
  --role "Senior Product Manager" \
  --url "https://jobs.ashbyhq.com/example/job-id"

# Cover letter from file
uv run python src/job_automation.py cover-letter \
  --company "Example Corp" \
  --role "Senior Product Manager" \
  --jd-file job-description.txt

# Generate both and archive to VPS
uv run python src/job_automation.py documents generate \
  --url "https://jobs.ashbyhq.com/example/job-id" \
  --company "Example" \
  --job-title "Product Manager" \
  --email "candidate@example.com" \
  --archive
```

### 3. Apply to Jobs

```powershell
# SAFE: Dry run (default)
uv run python src/job_automation.py apply \
  --url "https://jobs.ashbyhq.com/example/job-id" \
  --company "Example" \
  --role "Product Manager"

# SAFE: Fill form, inspect, no submit
uv run python src/job_automation.py apply \
  --url "https://jobs.ashbyhq.com/example/job-id" \
  --fill-only --headed

# LIVE: Actually submit
uv run python src/job_automation.py apply \
  --url "https://jobs.ashbyhq.com/example/job-id" \
  --company "Example" \
  --role "Product Manager" \
  --live-submit
```

### 4. Tracker-Based Application

```powershell
# Dry run with tracker
uv run python src/job_automation.py apply \
  --tracker data/workbooks/greenhouse_product_management_jobs.xlsx \
  --limit 5 \
  --dry-run

# Live with tracker
uv run python src/job_automation.py apply \
  --tracker data/workbooks/greenhouse_product_management_jobs.xlsx \
  --limit 5 \
  --live-submit \
  --no-shuffle

# With custom resume
uv run python src/job_automation.py apply \
  --tracker data/workbooks/greenhouse_product_management_jobs.xlsx \
  --resume output/my-resume.pdf \
  --limit 3 \
  --dry-run
```

### 5. Queue Processing

```powershell
# Create queue file (jobs.txt)
# One URL per line:
# https://jobs.ashbyhq.com/example/job1
# https://boards.greenhouse.io/company/jobs/job2

# Process queue (ALWAYS LIVE)
uv run python src/job_automation.py queue \
  --queue .\jobs.txt

# Resume from index 3
uv run python src/job_automation.py queue \
  --queue .\jobs.txt \
  --start-index 3

# With custom timeout (seconds)
uv run python src/job_automation.py queue \
  --queue .\jobs.txt \
  --timeout 300
```

### 6. VPS Document Archive

```powershell
# Store documents (dry run)
uv run python src/job_automation.py documents store \
  --url "https://jobs.example.com/job-id" \
  --company "Example" \
  --job-title "Product Manager" \
  --email "candidate@example.com" \
  --cv ".\output\resume.pdf" \
  --cover-letter ".\output\cover_letter.pdf"

# Store documents (execute)
uv run python src/job_automation.py documents store \
  --url "https://jobs.example.com/job-id" \
  --company "Example" \
  --job-title "Product Manager" \
  --email "candidate@example.com" \
  --cv ".\output\resume.pdf" \
  --cover-letter ".\output\cover_letter.pdf" \
  --execute

# Retrieve documents
uv run python src/job_automation.py documents retrieve \
  --url "https://jobs.example.com/job-id" \
  --company "Example" \
  --job-title "Product Manager" \
  --email "candidate@example.com"

# Generate and archive in one step
uv run python src/job_automation.py documents generate \
  --url "https://jobs.example.com/job-id" \
  --company "Example" \
  --job-title "Product Manager" \
  --email "candidate@example.com" \
  --archive
```

### 7. Gmail Operations

```powershell
# Read recent emails
uv run python src/job_automation.py gmail \
  --query "newer_than:7d" \
  --include-body

# Read unread emails
uv run python src/job_automation.py gmail \
  --unread \
  --classify

# Export to CSV (redacted)
uv run python src/job_automation.py gmail \
  --query "newer_than:30d" \
  --csv output\mail.csv \
  --redact

# Export to JSON
uv run python src/job_automation.py gmail \
  --query "newer_than:30d" \
  --json output\mail.json \
  --redact

# Create draft
uv run python src/job_automation.py gmail \
  --send-to "recipient@example.com" \
  --subject "Hello" \
  --body "Message body" \
  --draft

# Send email (with confirmation)
uv run python src/job_automation.py gmail \
  --send-to "recipient@example.com" \
  --subject "Hello" \
  --body "Message body"

# Send without confirmation (CAUTION)
uv run python src/job_automation.py gmail \
  --send-to "recipient@example.com" \
  --subject "Hello" \
  --body "Message body" \
  --yes
```

### 8. Email Pool

```powershell
# Select one email
uv run python src/job_automation.py email-pool --count 1

# Select multiple emails
uv run python src/job_automation.py email-pool --count 3

# Use custom pool
uv run python src/job_automation.py email-pool \
  --file custom_emails.json \
  --count 2
```

### 9. Google Indexing

```powershell
# Validate sitemap config (dry run)
uv run python src/job_automation.py google-indexing sitemap --dry-run

# Submit sitemap
uv run python src/job_automation.py google-indexing sitemap

# Submit URL (dry run)
uv run python src/job_automation.py google-indexing submit \
  --url "https://skybison.cloud/jobs/example" \
  --type URL_UPDATED \
  --dry-run

# Submit URL
uv run python src/job_automation.py google-indexing submit \
  --url "https://skybison.cloud/jobs/example" \
  --type URL_UPDATED

# Check URL status
uv run python src/job_automation.py google-indexing status \
  --url "https://skybison.cloud/jobs/example"
```

## 10. VPS Operations

```powershell
# Check worker status

# Audit VPS runtime

# Pull reports from VPS

# Install continuous workers

# Install dashboard

# Install memory guard

# Prune old outputs (dry run)

# Prune old outputs (delete)
```

##  Configuration Quick Reference

### candidate_profile_config.json

```json
{
  "candidate": {
    "first_name": "John",
    "last_name": "Doe",
    "email": "john@example.com",
    "phone": "+1234567890",
    "linkedin_url": "https://linkedin.com/in/johndoe",
    "portfolio_url": "https://johndoe.com",
    "location": "San Francisco, CA"
  },
  "policies": {
    "answers": {
      "work_authorization": "Authorized to work in US",
      "work_authorization_countries": ["US", "CA"],
      "eeo_race": "Prefer not to say",
      "eeo_gender": "Prefer not to say",
      "eeo_veteran": "No",
      "eeo_disability": "Prefer not to say"
    },
    "navigation_timeout_ms": 30000,
    "action_timeout_ms": 10000,
    "attempts": 3
  }
}
```

### candidate_email_pool.json

```json
[
  "john@example.com",
  "john.applications@example.com",
  "john.jobs@example.com"
]
```

### runtime/application.json

```json
{
  "tracker_path": "data/workbooks/greenhouse_product_management_jobs.xlsx",
  "resume_source_path": "data/resumes/base-resume.txt",
  "output_dir": "output",
  "email_pool_path": "config/candidate_email_pool.json",
  "application_timeout_seconds": 300,
  "resume_timeout_seconds": 120,
  "queue_timeout_seconds": 300,
  "vps_max_document_jobs": 10,
  "vps_document_retry_jobs": 3,
  "vps_max_attempts_per_ats": 5
}
```

##  File Locations

```
Project Root
├── config/                    # Configuration
│   ├── candidate_profile_config.json    # Your profile
│   ├── candidate_email_pool.json        # Email addresses
│   ├── vertex_service_account.json     # Vertex AI
│   ├── credentials.json               # Gmail OAuth client
│   ├── token.json                    # Gmail OAuth token
│   ├── vps_config.json               # VPS archive config
│   ├── seo_config.json               # Google indexing
│   ├── runtime_config.json           # Runtime defaults
│   └── runtime/                      # Operational defaults
│       ├── application.json
│       ├── browser.json
│       ├── vertex.json
│       ├── resume.json
│       ├── cover_letter.json
│       ├── search.json
│       ├── ashby.json
│       ├── gmail.json
│       ├── observability.json
│       └── continuous_worker.json
├── data/                       # Private inputs
│   ├── resumes/
│   │   └── base-resume.txt            # Your resume
│   └── workbooks/                    # Tracker files
├── output/                     # Generated artifacts
│   ├── ai_jobs.csv                 # Search results
│   ├── job_search_coverage.json  # Coverage report
│   ├── ats_boards_cache.json      # Board cache
│   ├── job_backlog.json           # Active jobs
│   ├── orchestration_results.json # Application results
│   ├── submission_log.json        # Confirmed submissions
│   ├── job_url_queue_progress.json # Queue state
│   ├── application_documents/    # Generated PDFs
│   ├── retrieved_documents/      # Retrieved from VPS
│   └── vps_reports/              # Pulled from VPS
└── src/                        # Source code
```

##  Exit Codes

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Success | Continue |
| 1 | Workflow/remote failure | Check logs, retry |
| 2 | Invalid input | Fix command, retry |
| 3 | Gmail API error | Check auth, retry |
| 4 | Auth/config error | Check config files |
| 130 | Interrupted | Retry command |

##  Safety Checklist

Before running any live workflow:

- [ ] Using `--dry-run` or `--fill-only` for testing
- [ ] Verified candidate profile is complete
- [ ] Verified email pool has valid addresses
- [ ] Base resume exists and is valid
- [ ] Configuration files are in place
- [ ] For VPS: SSH keys and host key are configured
- [ ] For Gmail: OAuth credentials are valid
- [ ] For AI: Vertex service account is configured

Before treating an application as submitted:

- [ ] Result shows `"SUBMITTED & CONFIRMED"`
- [ ] Entry exists in `output/submission_log.json`
- [ ] Verified with employer if uncertain

##  Supported ATS Platforms

| Platform | Search | Application | Notes |
|----------|--------|-------------|-------|
| Ashby |  |  | Full support |
| Greenhouse |  |  | Full support |
| Lever |  |  | Full support |
| SmartRecruiters |  |  | Full support |
| Workable |  |  | Full support |
| Other (JSON-LD) |  |  | Discovery only |

##  Common Flags

### Global Flags (most commands)

```
--help, -h          Show help
--verbose, -v      Verbose output
--config           Config file path
--output           Output directory
```

### Search Flags

```
--role-type         Job role/title (repeatable)
--ats-platform      ATS platform (repeatable)
--location          Location filter (repeatable)
--verify-live       Check if jobs are live
--require-live      Only return live jobs
--days              Rolling window in days
--posted-since      Start date (YYYY-MM-DD)
--posted-until      End date (YYYY-MM-DD)
--board-url         Specific board URL
--boards-file       File with board URLs
--career-page       Company career page
--career-pages-file File with career pages
--backlog-output    Backlog file path
--json-output       JSON output path
```

### Apply Flags

```
--url              Job URL
--tracker          Tracker file path
--company          Company name
--role             Job role
--dry-run          No submission (default)
--fill-only        Fill form, no submit
--live-submit      Actually submit
--headed           Show browser
--limit            Max jobs to process
--start-index      Start from row/index
--no-shuffle       Preserve order
--timeout          Timeout in seconds
--resume           Custom resume path
--config           Runtime config path
--results-file     Results output path
--submission-log-file Submission log path
```

### Gmail Flags

```
--query            Search query
--unread           Only unread messages
--all-mail         Include all mail
--include-body     Include message body
--classify         Classify messages
--csv              CSV export
--json             JSON export
--redact           Redact sensitive data
--send-to          Recipient email
--subject          Email subject
--body             Email body
--draft            Create draft
--yes              Skip confirmation
```

---

**See Also:**
- [Full Documentation](README.md)
- [FAQ](faq.md)
- [Operations Runbook](operations-runbook.md)
- [CLI Reference](cli-reference.md)
