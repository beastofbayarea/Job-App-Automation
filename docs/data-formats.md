# Data formats

All JSON artifacts are UTF-8. Persisted JSON, CSV, and text outputs are written atomically. Paths below are defaults and can be overridden by CLI or runtime configuration.

## Application tracker

The tracker is an `.xlsx` workbook. The orchestrator reads the active worksheet and recognizes these case-insensitive header names:

| Value | Recognized headers |
| --- | --- |
| Company | `company`, `company name` |
| Role | `title`, `job title`, `role`, `role title` |
| URL | `url`, `job url`, `link`, `job link` |

Rows without a job-specific URL for a supported provider are skipped. Supported providers are Ashby, Greenhouse, Lever, Workable, and SmartRecruiters. Company-board roots are not application identities. Missing company and role cells fall back to `Company` and `Product Manager`.

### Private job URL inventories

The private, Git-ignored URL inventories in `data/` use these canonical names:

| File | Worksheet(s) |
| --- | --- |
| `ashby_product_management_jobs.xlsx` | `Ashby Product Management` |
| `greenhouse_all_jobs.xlsx` | `Greenhouse All Roles` |
| `greenhouse_marketing_jobs.xlsx` | `Greenhouse Growth Marketing` |
| `greenhouse_product_management_jobs.xlsx` | `Greenhouse Product Management` |
| `lever_product_management_jobs.xlsx` | `Lever Product Management` |
| `smartrecruiters_and_workable_jobs.xlsx` | `SmartRecruiters Product Roles`, `Workable Product Roles` |

The prompt instructions formerly embedded beside the job tables are preserved
verbatim in the private, Git-ignored `data/job_search_prompts.txt` archive and
grouped by their original workbook and worksheet.

Run `npm install` once and then `npm run workbooks:clean` to normalize headers,
remove unused ranges and confirmed-closed listings, preserve valid HTTPS links,
and reapply the standard table format. The command validates every generated
workbook before replacing a source file and moves recoverable originals to
`output/workbook-backups/<timestamp>/`. Run `npm run workbooks:check` for a
read-only structural validation of the canonical files.

## Queue file

The queue is UTF-8 text with one job URL per non-empty line:

```text
https://jobs.ashbyhq.com/example/posting-id
https://boards.greenhouse.io/example/jobs/posting-id
```

The queue preserves line order. `--start-index` is zero-based.

## Search results

`output/ai_jobs.csv` and optional JSON output use this stable field order:

```text
platform, company, title, posted_at, days_old, location,
workplace_type, employment_type, department, team, salary,
job_url, apply_url, board_token, date_source, match_reason,
platform_job_id, live_status, live_checked_at, live_check_source,
live_check_http_status, live_check_final_url, live_check_reason
```

The JSON output is an array of objects with the same public fields. An empty value means the source did not provide a reliable value; it does not imply a negative result.

## Active job backlog

`output/job_backlog.json` is the persistent active, unsubmitted list used by
the VPS search workflow. Its root is:

```json
{
  "version": 1,
  "updated_at": "2026-07-31T12:00:00+00:00",
  "jobs": []
}
```

Each `jobs` entry contains the public search-result fields plus
`board_region`, `provider_id_trusted`, `source_identity`,
`url_is_record_specific`, `unique_id`, `first_seen_at`, and `last_seen_at`.
These identity fields let a role that was not rediscovered be checked against
its provider again. `days_old` remains the value observed when that record was
last refreshed.

The file never stores descriptions, candidate data, application state,
failure diagnostics, documents, email addresses, or submission evidence. A
search merges new roles, preserves the earliest `first_seen_at`, refreshes
`last_seen_at` when rediscovered, and removes only:

- a URL matching an exact `SUBMITTED & CONFIRMED` submission-ledger entry; or
- a role whose provider or job page conclusively reports `closed`.

`unknown`, `not_checked`, `listed`, and `live` records remain. There is no
archive or deletion-history structure; a genuinely reopened role may re-enter,
while a confirmed role remains excluded by the permanent submission ledger.

## Search coverage report

`output/job_search_coverage.json` has `version`, `generated_at`, `criteria`, `cache`, `discovery`, `feed_fetch`, `fallback`, `backlog`, and `results` objects. The `results.returned` value is the current-run count written after filtering; `results.live_status_counts` summarizes those liveness outcomes. When backlog persistence is enabled, `backlog` reports migration, candidate, removal, and retained counts. Use discovery and feed statistics to distinguish “no matching jobs” from incomplete source coverage.

## Orchestration results

`output/orchestration_results.json` is an array written after each processed job. Every record starts with:

```json
{
  "row": 2,
  "company": "Example",
  "role": "Product Manager",
  "url": "https://…",
  "ats": "greenhouse"
}
```

Completed engine results add `success`, `status`, `submitted`, `confirmed`, and `test_mode`, plus engine, resume, masked email, errors, or provider-specific diagnostics when available. Only the exact `SUBMITTED & CONFIRMED` state with successful, submitted, and confirmed flags and `test_mode: false` is safe to count as a completed application.

## Submission log

`output/submission_log.json` is an object whose first same-day company/role entry uses `YYYYMMDD-company-role`. A short deterministic suffix prevents a distinct same-day application from overwriting that entry. Entries contain:

```json
{
  "applied_at": "2026-07-28T12:00:00+00:00",
  "company": "Example",
  "role": "Product Manager",
  "job_url": "https://…",
  "ats": "greenhouse",
  "status": "SUBMITTED & CONFIRMED",
  "email_used": "candidate@example.com",
  "resume_filename": "example_product_manager.pdf",
  "cover_letter_filename": "",
  "remote_path": ""
}
```

The log stores confirmed submissions only. It contains personal data and must not be committed or shared without authorization.

## Private document archive manifest

Each VPS record stores `resume.pdf`, `cover_letter.pdf`, `manifest.json`, and an internal fingerprint file below an opaque path such as `records/ab/ja1_<sha256>`. The manifest schema is:

```json
{
  "schema_version": 1,
  "identity_version": 1,
  "archive_id": "ja1_<sha256>",
  "record_fingerprint": "<sha256>",
  "created_at": "2026-07-28T12:00:00+00:00",
  "identity": {
    "job_url": "https://jobs.example.com/role-id",
    "canonical_job_url": "https://jobs.example.com/role-id",
    "company": "Example",
    "company_key": "example",
    "job_title": "Product Manager",
    "job_title_key": "product manager",
    "email_used": "candidate@example.com",
    "email_key": "candidate@example.com"
  },
  "documents": {
    "resume": {
      "kind": "resume",
      "stored_name": "resume.pdf",
      "original_filename": "reviewed-resume.pdf",
      "sha256": "<sha256>",
      "size_bytes": 12345,
      "media_type": "application/pdf"
    },
    "cover_letter": {
      "kind": "cover_letter",
      "stored_name": "cover_letter.pdf",
      "original_filename": "reviewed-cover-letter.pdf",
      "sha256": "<sha256>",
      "size_bytes": 6789,
      "media_type": "application/pdf"
    }
  }
}
```

The URL and normalized email derive the opaque archive ID. Retrieval also requires exact normalized company and job-title matches. The manifest and PDFs are private PII and must never be committed.

## Queue progress

`output/job_url_queue_progress.json` records `queue_count`, zero-based `last_index`, `last_url`, `confirmed`, and the complete `result` object. It represents the latest attempted item, not a list of all attempts.

## Candidate email pool

`config/candidate_email_pool.json` is a JSON array of email-address strings:

```json
[
  "candidate@example.com",
  "candidate+applications@example.com"
]
```
