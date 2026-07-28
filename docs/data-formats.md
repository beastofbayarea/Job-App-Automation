# Data formats

All JSON artifacts are UTF-8. Persisted JSON, CSV, and text outputs are written atomically. Paths below are defaults and can be overridden by CLI or runtime configuration.

## Application tracker

The tracker is an `.xlsx` workbook. The orchestrator reads the active worksheet and recognizes these case-insensitive header names:

| Value | Recognized headers |
| --- | --- |
| Company | `company`, `company name` |
| Role | `title`, `job title`, `role`, `role title` |
| URL | `url`, `job url`, `link`, `job link` |

Rows without a supported HTTPS Ashby, Greenhouse, or Lever URL are skipped. Missing company and role cells fall back to `Company` and `Product Manager`.

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

## Search coverage report

`output/job_search_coverage.json` has `version`, `generated_at`, `criteria`, `cache`, `discovery`, `feed_fetch`, `fallback`, and `results` objects. The `results.returned` value is the number written after filtering; `results.live_status_counts` summarizes liveness outcomes. Use discovery and feed statistics to distinguish “no matching jobs” from incomplete source coverage.

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

Completed engine results add `success`, `status`, `submitted`, `confirmed`, and `test_mode`, plus engine, resume, masked email, errors, or provider-specific diagnostics when available. Only the exact `SUBMITTED & CONFIRMED` state with successful, submitted, and confirmed flags is safe to count as a completed application.

## Submission log

`output/submission_log.json` is an object keyed by `YYYYMMDD-company-role`. Entries contain:

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
