---
name: greenhouse-job-search
description: Discover, verify, filter, deduplicate, and save current Greenhouse job postings as JSON. Use for fresh Greenhouse searches driven by data/templates/job-search-prompt.txt or equivalent role, location, recency, industry, and one-role-per-company requirements.
---

# Greenhouse Job Search

Create a new search artifact from scratch. Do not use an existing operational
queue as the candidate source unless the user explicitly requests a refresh.

## Workflow

For expanded searches, collect board tokens from current and stale indexed
results, then scan every employer's full public feed at
`https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true`.
Filter locally before verifying individual postings; this finds jobs that search
engines have not indexed.

1. Read the requested prompt completely. Inspect the destination only to
   determine naming and format; preserve existing files.
2. Search `job-boards.greenhouse.io`, `job-boards.eu.greenhouse.io`, and legacy
   `boards.greenhouse.io` results across every role group and several synonyms.
3. Extract the board token and numeric job ID from each candidate.
4. Verify each candidate through
   `https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}`. Require HTTP
   200 and an identical `id`.
5. Use `first_published` as the authoritative date. Use `updated_at` only when
   `first_published` is unavailable, and record that uncertainty during review.
   Reject elapsed `application_deadline` values.
6. Inspect the full job content and apply every prompt constraint. Reject old,
   expired, excluded-location, excluded-industry, clearance-required, internship,
   evergreen, and talent-pipeline roles. Retain one strongest role per company.
7. Prefer prompt locations in their stated order. If Greenhouse publishes
   location-specific duplicates, choose the preferred eligible location.
8. Canonicalize to
   `https://job-boards.greenhouse.io/{board}/jobs/{id}` or the EU equivalent.
   Remove query parameters and fragments. Preserve `gh_jid` only when the job ID
   is not present in the path.
9. Sort newest to oldest and write a new five-field JSON file:

```json
[
  {
    "posting_date": "YYYY-MM-DD",
    "company": "Company",
    "title": "Job title",
    "location": "Remote - United States",
    "url": "https://job-boards.greenhouse.io/company/jobs/1234567"
  }
]
```

10. Run `scripts/Test-GreenhouseJobSearch.ps1 -VerifyLive` against the result.
    Confirm that only the intended new output changed.

## Evidence rules

- Omit uncertain candidates; never use a search crawl date as the posting date.
- Treat the individual job API as authoritative for identity, date, title,
  content, deadline, and current availability.
- Keep search artifacts separate from operational application statuses.
- Respect repository ignore rules; do not force-add private queue artifacts.
