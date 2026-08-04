---
name: lever-job-search
description: Discover, verify, filter, deduplicate, and save current Lever job postings as JSON. Use for fresh Lever searches driven by data/templates/job-search-prompt.txt or equivalent role, location, recency, industry, and one-role-per-company requirements.
---

# Lever Job Search

Create a new search artifact from scratch. Do not use an existing operational
queue as the candidate source unless the user explicitly requests a refresh.

## Workflow

1. Read the requested prompt completely. Inspect the destination only to
   determine naming and format; preserve existing files.
2. Search for individual postings on `jobs.lever.co` and `jobs.eu.lever.co`
   across every role group. Use multiple role synonyms and queries.
3. Extract the board slug and UUID from each candidate URL.
4. Verify every candidate through the matching individual-posting endpoint:
   - Global: `https://api.lever.co/v0/postings/{board}/{id}?mode=json`
   - EU: `https://api.eu.lever.co/v0/postings/{board}/{id}?mode=json`
   Require HTTP 200 and an identical `id`.
5. Use `createdAt` as the authoritative posting date, converting its Unix
   milliseconds to UTC. If it is absent, use page JSON-LD `datePosted`; never
   use a search-engine crawl date. Inspect all description and `lists` content
   for role fit and exclusions.
6. Apply every prompt constraint:
   - reject postings older than the specified calendar window;
   - reject excluded locations, industries, talent pools, internships,
     expired roles, and clearance-required roles;
   - distinguish incidental benefit language from the employer's industry;
   - retain active individual postings only;
   - choose one strongest match per company;
   - prefer locations in the prompt's stated order.
7. Canonicalize each URL to
   `https://jobs.lever.co/{board}/{id}` or its EU equivalent. Remove `/apply`,
   query parameters, and fragments. Never save a board landing page.
8. Sort newest to oldest and write a new JSON file. For the standard prompt,
   use exactly:

```json
[
  {
    "posting_date": "YYYY-MM-DD",
    "company": "Company",
    "title": "Job title",
    "location": "Remote - United States",
    "url": "https://jobs.lever.co/company/00000000-0000-0000-0000-000000000000"
  }
]
```

9. Run `scripts/Test-LeverJobSearch.ps1 -VerifyLive` against the result.
   Confirm that only the intended new output changed.

## Evidence rules

- Omit uncertain candidates; do not infer dates or liveness from indexed pages.
- Treat the individual posting API as the source of truth for identity, date,
  title, content, and current availability.
- Keep search artifacts separate from queue statuses such as `NOT_ATTEMPTED`,
  `FAILED`, or `CAPTCHA_REQUIRED`.
- Respect repository ignore rules for private queue data. Do not force-add an
  ignored artifact unless the user explicitly requests public synchronization.
