---
name: smartrecruiters-job-search
description: Discover, verify, filter, deduplicate, and save current SmartRecruiters job postings as JSON. Use for fresh SmartRecruiters searches driven by data/templates/job-search-prompt.txt or equivalent role, location, recency, industry, and one-role-per-company requirements.
---

# SmartRecruiters Job Search

Create a new search artifact from scratch. Do not use an existing operational
queue as the candidate source unless the user explicitly requests a refresh.

## Workflow

1. Read the requested prompt completely. Inspect the destination only to
   determine naming and format; preserve existing files.
2. Search for individual `jobs.smartrecruiters.com` postings across every role
   group. Use multiple role synonyms and queries.
3. Extract the company slug and numeric posting ID from each candidate URL.
4. Verify every candidate through:
   `https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}`.
   Require HTTP 200, an identical `id`, and a public posting response.
5. Use `releasedDate` as the authoritative posting/reposting date. Use
   `location` for city, region, country, and remote status. Inspect the
   description, qualifications, additional information, and `industry.label`
   for role fit and exclusions.
6. Apply every prompt constraint:
   - reject postings older than the specified calendar window;
   - reject excluded locations, industries, talent pools, internships,
     expired roles, and clearance-required roles;
   - retain active individual postings only;
   - choose one strongest match per company;
   - prefer locations in the prompt's stated order.
7. Canonicalize each direct job URL to
   `https://jobs.smartrecruiters.com/{company}/{id}`. Remove title slugs, query
   parameters, and fragments. Never save a company landing page.
8. Sort newest to oldest and write a new JSON file. For the standard prompt,
   use exactly:

```json
[
  {
    "posting_date": "YYYY-MM-DD",
    "company": "Company",
    "title": "Job title",
    "location": "Remote - United States",
    "url": "https://jobs.smartrecruiters.com/Company/123456789"
  }
]
```

9. Run `scripts/Test-SmartRecruitersJobSearch.ps1 -VerifyLive` against the
   result. Confirm that only the intended new output changed.

## Evidence rules

- Omit uncertain candidates; never infer exact dates or liveness from search
  result crawl dates.
- Treat the public posting API as the source of truth for identity, date,
  location, content, and current availability.
- Keep search artifacts separate from queue statuses such as `NOT_ATTEMPTED`,
  `FAILED`, or `CAPTCHA_REQUIRED`.
- Respect repository ignore rules for private queue data. Do not force-add an
  ignored artifact unless the user explicitly requests public synchronization.
