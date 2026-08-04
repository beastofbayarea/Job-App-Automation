---
name: workable-job-search
description: Discover, verify, filter, deduplicate, and save current Workable job postings as JSON. Use for fresh Workable searches driven by data/templates/job-search-prompt.txt or equivalent role, location, recency, industry, and one-role-per-company requirements.
---

# Workable Job Search

Create a new search artifact from scratch. Do not treat an existing queue as the
candidate source unless the user explicitly asks to refresh it.

## Workflow

1. Read the requested prompt completely and inspect the destination only to
   determine naming and format. Preserve existing files.
2. Search the web for individual `apply.workable.com` postings across every
   requested role group. Use several role synonyms; one broad query is not
   exhaustive.
3. Extract the account slug and job shortcode from each candidate.
4. Verify the candidate in the current public account feed:
   `https://www.workable.com/api/accounts/{account}?details=true`.
   The shortcode must appear in `jobs`, and `published_on` is the authoritative
   posting/reposting date.
5. If direct requests are throttled, retry conservatively. A read-only text
   proxy such as
   `https://r.jina.ai/http://www.workable.com/api/accounts/{account}?details=true`
   may be used to inspect the same public feed. Never infer an exact date from a
   search-engine crawl date.
6. Apply every prompt constraint:
   - reject stale postings using the run date and requested age window;
   - reject excluded locations, industries, talent pools, internships,
     expired roles, and clearance-required roles;
   - retain active individual postings only;
   - choose one strongest match per company;
   - prefer locations in the prompt's stated order.
7. Canonicalize each URL to the specific posting and remove its query and
   fragment. Prefer `https://apply.workable.com/j/{shortcode}` when the feed
   supplies it. Never save an account landing page.
8. Sort newest to oldest and write a new JSON file. For the standard prompt,
   use exactly:

```json
[
  {
    "posting_date": "YYYY-MM-DD",
    "company": "Company",
    "title": "Job title",
    "location": "Remote - United States",
    "url": "https://apply.workable.com/j/ABC123"
  }
]
```

9. Run `scripts/Test-WorkableJobSearch.ps1` against the result. Also confirm
   that only the intended new file changed.

## Evidence rules

- Omit an uncertain candidate; do not invent dates, locations, or liveness.
- Treat the Workable account feed as liveness evidence, not the search result.
- Keep public search artifacts separate from operational queue statuses such
  as `NOT_ATTEMPTED`, `FAILED`, or `CAPTCHA_REQUIRED`.
- Respect repository ignore rules for private queue data. Do not force-add an
  ignored artifact unless the user explicitly requests public synchronization.

