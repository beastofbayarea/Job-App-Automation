---
name: ashby-job-search
description: Discover, verify, filter, deduplicate, and save current Ashby job postings as JSON. Use for fresh Ashby searches driven by data/templates/job-search-prompt.txt or equivalent role, location, recency, industry, and one-role-per-company requirements.
---

# Ashby Job Search

Create a new search artifact from scratch. Do not use an existing operational
queue as the candidate source unless the user explicitly requests a refresh.

## Workflow

1. Read the prompt completely. Inspect the destination only for naming and
   format, and preserve existing files.
2. Search for individual `jobs.ashbyhq.com/{board}/{uuid}` postings across
   every requested role group using multiple role synonyms.
3. Extract the company board token and posting UUID from every candidate URL.
4. Fetch `https://api.ashbyhq.com/posting-api/job-board/{board}` and require
   the exact UUID to be present with `isListed` other than `false`.
5. Treat API `publishedAt` as the authoritative posting or reposting date.
   Use `location`, `secondaryLocations`, and `workplaceType` for location.
   Inspect `descriptionPlain` or `descriptionHtml` for role fit and exclusions.
6. Apply all prompt constraints:
   - compare dates using the requested calendar window, not search crawl age;
   - reject excluded locations, industries, internships, talent pools,
     clearance requirements, and unlisted jobs;
   - distinguish incidental benefit language from the employer's industry;
   - choose one strongest role per company;
   - prefer locations in the prompt's stated order.
7. Canonicalize URLs to `https://jobs.ashbyhq.com/{board}/{uuid}`. Remove
   trailing slashes, `/application`, `/apply`, query parameters, and fragments.
8. Sort newest to oldest and save a new JSON file with exactly:

```json
[
  {
    "posting_date": "YYYY-MM-DD",
    "company": "Company",
    "title": "Job title",
    "location": "Remote - United States",
    "url": "https://jobs.ashbyhq.com/company/00000000-0000-0000-0000-000000000000"
  }
]
```

9. Run `scripts/Test-AshbyJobSearch.ps1 -VerifyLive` against the result and
   confirm only the intended new artifact changed.

## Evidence rules

- Omit uncertain candidates; do not use indexed crawl dates as posting dates.
- Treat the current public board response as the source of truth for identity,
  liveness, title, date, location, and content.
- Keep search artifacts separate from operational application status fields.
- Respect repository ignore rules for private queue data. Do not force-add an
  ignored artifact unless the user explicitly requests public synchronization.
