# Frequently Asked Questions

This document addresses common questions about the Job Application Automation toolkit. For detailed procedures, see the [Operations Runbook](operations-runbook.md). For troubleshooting specific issues, see the [Troubleshooting Guide](troubleshooting.md).

## General Questions

### What is this tool?

This is a **local, safety-first toolkit** for:
- Discovering public ATS (Applicant Tracking System) job vacancies
- Generating tailored PDF resumes and cover letters
- Privately archiving documents on a VPS
- Filling applications on supported ATS providers (Ashby, Greenhouse, Lever, SmartRecruiters, Workable)

It includes Gmail OAuth utilities and candidate-email pool selection.

### Is this safe to use?

**Yes, if used responsibly.** The toolkit is designed with safety as a priority:

-  **Dry-run by default**: `apply` commands require explicit `--live-submit` to actually submit
-  **Confirmation required**: Only exact `SUBMITTED & CONFIRMED` results are counted
-  **No automatic retries**: Failed applications are quarantined for review
-  **Data protection**: Credentials and personal data are gitignored

**However, you must:**
- Only automate applications you're authorized to make
- Review every application before submission
- Keep credentials secure
- Follow the [Security & Privacy](security-and-privacy.md) guidelines

### What providers are supported?

| Provider | Search | Application |
|----------|--------|-------------|
| Ashby |  Yes |  Yes |
| Greenhouse |  Yes |  Yes |
| Lever |  Yes |  Yes |
| SmartRecruiters |  Yes |  Yes |
| Workable |  Yes |  Yes |
| Other (JSON-LD) |  Discovery only |  No |

See [ATS Support](ats-support.md) for details.

### Can I add support for a new ATS?

Yes! The system is designed for extensibility. See [Architecture](architecture.md#extension-points) for guidance on:
- Adding a search provider adapter
- Implementing an engine with the shared result contract
- Registering CLI entrypoints

## Installation & Setup

### What are the system requirements?

| Requirement | Purpose | Notes |
|-------------|---------|-------|
| Python 3.10+ | Runtime | Required |
| uv (recommended) | Package management | Optional but recommended |
| Chromium | Browser automation | Installed via Playwright |
| Git | Version control | For cloning and updates |

### Should I use pip or uv?

**Use uv** (recommended):
```powershell
uv sync --locked --no-dev
uv run playwright install chromium
```

Benefits:
- Reproducible dependency locking
- Faster dependency resolution
- Better cross-platform support

**Use pip** (alternative):
```powershell
python -m venv .venv
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### How do I install Playwright Chromium?

```powershell
# With uv
uv run playwright install chromium

# With pip
python -m playwright install chromium
```

This downloads Chromium for browser automation. It's required for application workflows.

### Where should I run this?

**Local development**: Your workstation for testing and manual runs
**VPS deployment**: A remote server for continuous, unattended operations

See [Operations Runbook](operations-runbook.md) for VPS setup procedures.

### What files do I need to create?

Required files (copy from examples):
```powershell
config\candidate_profile_config.json    # From .example.json
config\candidate_email_pool.json        # From .example.json
```

Required file (create manually):
```
data\resumes\base-resume.txt           # Your resume content
```

Optional files (for specific features):
```
config\vertex_service_account.json     # For AI resume generation
config\credentials.json               # For Gmail integration
config\token.json                    # Created during Gmail auth
config\vps_config.json               # For VPS document archive
```

## Configuration

### How do I configure my candidate profile?

1. Copy the example:
   ```powershell
   Copy-Item config\candidate_profile_config.example.json config\candidate_profile_config.json
   ```

2. Edit the file with your:
   - Personal information (name, contact, etc.)
   - Pre-approved answers to common application questions
   - Browser settings (timeouts, etc.)
   - Work authorization details

3. **Important**: Never commit this file to Git - it's in `.gitignore`

See [Configuration Guide](configuration.md) for all available options.

### How do I add multiple email addresses?

Edit `config/candidate_email_pool.json`:
```json
[
  "candidate@example.com",
  "candidate+applications@example.com",
  "candidate.jobs@example.com"
]
```

The system will randomly select from this pool for applications.

### How do I configure Vertex AI for resume generation?

1. Create a service account in Google Cloud Console
2. Download the JSON key file
3. Save as `config/vertex_service_account.json`
4. Ensure the service account has `roles/aiplatform.user` role
5. Enable Vertex AI API in your project

The system falls back to rule-based generation if AI is unavailable.

### How do I set up Gmail integration?

1. Create OAuth credentials in Google Cloud Console:
   - Application type: Desktop app
   - Add `https://developers.google.com/oauthplayground` as redirect URI
2. Download credentials as `config/credentials.json`
3. Run a Gmail command to trigger authorization:
   ```powershell
   uv run python src/job_automation.py gmail --query "newer_than:1d"
   ```
4. The first run will open a browser for authorization
5. Token will be saved as `config/token.json`

### Can I use environment variables for configuration?

Yes, for some settings. The runtime configuration supports:
- `GOOGLE_APPLICATION_CREDENTIALS` for Vertex AI
- `SENTRY_DSN` for optional telemetry (VPS only)

Most configuration should be in JSON files for auditability.

## Search & Discovery

### How do I search for jobs?

Basic search:
```powershell
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --ats-platform lever
```

Advanced search:
```powershell
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --role-type "Product Owner" \
  --ats-platform greenhouse lever ashby \
  --location "Remote" \
  --location "New York" \
  --verify-live \
  --require-live
```

### Why am I getting few/no results?

Check these common issues:

1. **Filters too narrow**: Broaden your role types and locations
2. **Date filters**: Remove `--posted-since` or use a wider range
3. **Platform limitations**: Some providers have limited public listings
4. **Liveness checks**: `--require-live` excludes uncertain results

Review `output/job_search_coverage.json` for detailed discovery statistics.

### How do I search specific companies?

Use `--career-page` or `--career-pages-file`:
```powershell
# Single company
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --career-page "https://company.com/jobs"

# Multiple companies from file
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --career-pages-file companies.txt
```

### How do I use known job boards?

Use `--board-url` or `--boards-file`:
```powershell
# Single board
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --board-url "https://boards.greenhouse.io/example"

# Multiple boards from file
uv run python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --boards-file greenhouse_boards.txt
```

### What's the difference between `--verify-live` and `--require-live`?

- `--verify-live`: Actively checks if each job is still open
- `--require-live`: Filters out jobs whose liveness cannot be confirmed

`--require-live` implies `--verify-live`.

### Where are search results saved?

- `output/ai_jobs.csv` - Main search results in CSV format
- `output/job_search_coverage.json` - Discovery and coverage statistics
- `output/ats_boards_cache.json` - Reusable board cache
- `output/job_backlog.json` - Persistent active job list (if enabled)

## Document Generation

### How do I generate a tailored resume?

```powershell
uv run python src/job_automation.py resume \
  --company "Example Corp" \
  --role "Senior Product Manager" \
  --url "https://jobs.ashbyhq.com/example/job-id"
```

You can also provide job description text directly:
```powershell
uv run python src/job_automation.py resume \
  --company "Example Corp" \
  --role "Senior Product Manager" \
  --keywords "product,management,agile" \
  --jd-overview "Build and scale products..." \
  --jd-resp "Define roadmap, work with engineers..." \
  --jd-req "5+ years experience, product sense..."
```

### How do I generate a cover letter?

```powershell
uv run python src/job_automation.py cover-letter \
  --company "Example Corp" \
  --role "Senior Product Manager" \
  --url "https://jobs.ashbyhq.com/example/job-id"
```

Or with a job description file:
```powershell
uv run python src/job_automation.py cover-letter \
  --company "Example Corp" \
  --role "Senior Product Manager" \
  --jd-file job-description.txt
```

### How do I generate both resume and cover letter together?

```powershell
uv run python src/job_automation.py documents generate \
  --url "https://jobs.ashbyhq.com/example/job-id" \
  --company "Example" \
  --job-title "Product Manager" \
  --email "candidate@example.com"
```

### Can I use my own resume template?

The system uses a built-in template for consistency. To customize:

1. Modify the resume generation code in `src/job_application_automation/resume/`
2. Or use the generated PDF as input for your own formatting

### Where are generated documents saved?

By default: `output/application_documents/<id>/`

Each set includes:
- `resume.pdf` - Tailored resume
- `cover_letter.pdf` - Tailored cover letter
- `cover_letter.audit.json` - Evidence and validation data

## Application Workflows

### How do I apply to a job?

**Safe approach (recommended):**

1. Dry run first:
   ```powershell
   uv run python src/job_automation.py apply \
     --url "https://jobs.ashbyhq.com/example/job-id" \
     --dry-run
   ```

2. Fill form without submitting:
   ```powershell
   uv run python src/job_automation.py apply \
     --url "https://jobs.ashbyhq.com/example/job-id" \
     --fill-only --headed
   ```

3. Live submission (after review):
   ```powershell
   uv run python src/job_automation.py apply \
     --url "https://jobs.ashbyhq.com/example/job-id" \
     --live-submit
   ```

### What's the difference between `--dry-run`, `--fill-only`, and `--live-submit`?

| Mode | Behavior | Submits? |
|------|----------|----------|
| `--dry-run` (default) | Validates and plans, no browser |  No |
| `--fill-only` | Opens browser, fills form, no submit |  No |
| `--live-submit` | Full workflow including submission |  Yes |

### How do I apply from a tracker/Excel file?

```powershell
uv run python src/job_automation.py apply \
  --tracker data/workbooks/greenhouse_product_management_jobs.xlsx \
  --limit 5 \
  --dry-run
```

Options:
- `--limit`: Number of jobs to process
- `--start-index`: Start from specific row
- `--no-shuffle`: Preserve tracker order
- `--headed`: Show browser

### How do I use the queue command?

1. Create a text file with one job URL per line (`jobs.txt`):
   ```
   https://jobs.ashbyhq.com/example/job1
   https://boards.greenhouse.io/company/jobs/job2
   ```

2. Process the queue:
   ```powershell
   uv run python src/job_automation.py queue \
     --queue .\jobs.txt
   ```

**Important**: Queue always uses `--live-submit` and stops at the first unconfirmed application.

### How do I resume a failed queue?

```powershell
uv run python src/job_automation.py queue \
  --queue .\jobs.txt \
  --start-index 3
```

Check `output/job_url_queue_progress.json` for the last attempted index.

### How do I know if an application was submitted?

Check these files:
- `output/submission_log.json` - Only confirmed submissions
- `output/orchestration_results.json` - All application attempts

Look for status: `"SUBMITTED & CONFIRMED"`

**Important**: A filled form is NOT a confirmed submission. Only exact confirmation evidence counts.

### Why did my application fail?

Common failure reasons:

| Status | Meaning | Action |
|--------|---------|--------|
| `FILLED_NOT_SUBMITTED` | Form filled but not submitted | Use `--live-submit` |
| `FAILED_REQUIRED_FIELD` | Missing answer in profile | Update candidate profile |
| `CAPTCHA_DETECTED` | Anti-bot verification | Manual review required |
| `TIMEOUT` | Browser timeout | Increase timeouts in profile |
| `SUBMIT_BUTTON_NOT_FOUND` | Page structure changed | Update engine or try manually |
| `CONFIRMATION_PRESENT_BEFORE_SUBMIT` | Already submitted | Verify with employer |

### Can I apply to the same job multiple times?

**No, and you shouldn't try.** The system:
- Tracks confirmed submissions in `output/submission_log.json`
- Skips URLs already in confirmed or attempted state
- Will not automatically retry failed applications

This prevents duplicate applications.

## VPS Operations

### How do I set up VPS document archiving?

1. On VPS, create archive directory:
   ```bash
   sudo install -d -m 0700 -o jobarchive -g jobarchive \
     /var/lib/job-application-automation/private-archive
   ```

2. Configure `config/vps_config.json`:
   ```json
   {
     "vps": {
       "host": "your-vps.example.com",
       "ssh_user": "jobarchive",
       "ssh_host_key": "aa:bb:cc:...",
       "document_archive_root": "/var/lib/job-application-automation/private-archive",
       "archive_private_key_file": "path/to/key.ppk"
     }
   }
   ```

3. Test with dry run:
   ```powershell
   uv run python src/job_automation.py documents store \
     --url "https://jobs.example.com/job-id" \
     --company "Example" \
     --job-title "Product Manager" \
     --email "candidate@example.com" \
     --cv ".\output\resume.pdf" \
     --cover-letter ".\output\cover_letter.pdf"
   ```

4. Add `--execute` to perform actual upload

## Gmail Integration

### How do I read emails?

```powershell
# Read recent messages
uv run python src/job_automation.py gmail \
  --query "newer_than:7d" \
  --include-body

# Read unread messages only
uv run python src/job_automation.py gmail \
  --unread \
  --classify
```

### How do I export emails?

```powershell
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
```

### How do I send an email?

```powershell
# Create draft (interactive confirmation)
uv run python src/job_automation.py gmail \
  --send-to "recipient@example.com" \
  --subject "Hello" \
  --body "Message body" \
  --draft

# Send directly (interactive confirmation)
uv run python src/job_automation.py gmail \
  --send-to "recipient@example.com" \
  --subject "Hello" \
  --body "Message body"

# Send without confirmation (use with caution)
uv run python src/job_automation.py gmail \
  --send-to "recipient@example.com" \
  --subject "Hello" \
  --body "Message body" \
  --yes
```

### How do I select a candidate email?

```powershell
# Select one random email
uv run python src/job_automation.py email-pool --count 1

# Select multiple emails
uv run python src/job_automation.py email-pool --count 3

# Use a custom pool file
uv run python src/job_automation.py email-pool \
  --file custom_emails.json \
  --count 2
```

## Troubleshooting

### Where are logs and outputs?

```
output/                      # All generated artifacts
  ai_jobs.csv                # Search results
  orchestration_results.json # Application results
  submission_log.json        # Confirmed submissions
  job_backlog.json           # Active jobs
  job_search_coverage.json  # Discovery statistics
  application_documents/    # Generated PDFs
  retrieved_documents/      # Retrieved from VPS
  job_url_queue_progress.json # Queue state
  google_url_submission_report.json # Google indexing
  vps_reports/              # Pulled from VPS
```

### Common error codes

| Code | Meaning | Document |
|------|---------|----------|
| 0 | Success | - |
| 1 | Workflow/remote failure | [Troubleshooting](troubleshooting.md) |
| 2 | Invalid input | Check command syntax |
| 3 | Gmail API error | [Troubleshooting](troubleshooting.md#gmail-authorization-fails) |
| 4 | Auth/config error | [Configuration](configuration.md) |
| 130 | Interrupted | Retry command |

### How do I get more help?

1. **Check the Troubleshooting Guide**: [troubleshooting.md](troubleshooting.md)
2. **Review the Operations Runbook**: [operations-runbook.md](operations-runbook.md)
3. **Verify your configuration**: [configuration.md](configuration.md)
4. **Consult the CLI reference**: [cli-reference.md](cli-reference.md)

---

## Best Practices

### For Safe Operations

1.  **Always start with dry-run**: Test every new workflow with `--dry-run` first
2.  **Review before submitting**: Use `--fill-only --headed` to inspect forms
3.  **Verify confirmation**: Check `submission_log.json` for confirmed submissions
4.  **Never bypass safety checks**: Don't use `--yes` or `--live-submit` without review
5.  **Queue is for live submissions only**: Don't use queue for testing

### For Data Management

1.  **Keep credentials secure**: Never commit personal data to Git
2.  **Use redacted exports**: Always use `--redact` for Gmail exports
3.  **Maintain backups**: Keep encrypted backups of VPS archives
4.  **Clean up regularly**: Remove old outputs and temporary files
5.  **Respect retention policies**: Delete data when no longer needed

### For VPS Operations

1.  **Use dedicated accounts**: Separate SSH accounts for archive operations
2.  **Pin host keys**: Always verify SSH host key fingerprints
3.  **Monitor workers**: Regularly check worker status and logs
4.  **Review failures**: Investigate quarantined applications
5.  **Update regularly**: Keep VPS code and dependencies current

---

**See Also:**
- [Documentation Index](README.md)
- [Operations Runbook](operations-runbook.md)
- [Troubleshooting Guide](troubleshooting.md)
- [Configuration Guide](configuration.md)
- [CLI Reference](cli-reference.md)
