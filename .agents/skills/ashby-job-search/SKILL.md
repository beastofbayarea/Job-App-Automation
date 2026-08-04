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
2. Search for individual `jobs.ashbyhq.com/{board}/{uuid}` postings using a
   query matrix rather than one query per role group:
   - product: product manager, technical product manager, AI product manager,
     staff/principal/group product manager, product operations, and product
     director;
   - program: technical program manager and product-focused program manager;
   - marketing: growth, performance, paid media, marketing operations, demand
     generation, product marketing, and GTM marketing;
   - investment/strategy: corporate development, venture capital, investment,
     management consultant, and strategy consultant;
   - location: remote US, remote Europe/UK, France, UAE/Abu Dhabi, India,
     Australia, and Singapore.
   Run both title-specific and title-plus-location searches. Continue until a
   pass through the matrix yields no new qualifying companies.
3. Build a unique board-token set from every result, including stale or closed
   indexed postings. Fetch each board's full public API inventory and scan all
   currently listed jobs for the title matrix and date window. This often
   reveals newly published jobs that search engines have not indexed. Repeat
   after newly discovered boards are added.
4. Extract the company board token and posting UUID from every candidate URL.
5. Fetch `https://api.ashbyhq.com/posting-api/job-board/{board}` and require
   the exact UUID to be present with `isListed` other than `false`.
6. Treat API `publishedAt` as the authoritative posting or reposting date.
   Use `location`, `secondaryLocations`, and `workplaceType` for location.
   Inspect `descriptionPlain` or `descriptionHtml` for role fit and exclusions.
7. Apply all prompt constraints:
   - compare dates using the requested calendar window, not search crawl age;
   - reject excluded locations, industries, internships, talent pools,
     clearance requirements, and unlisted jobs;
   - distinguish incidental benefit language from the employer's industry;
   - choose one strongest role per company;
   - prefer locations in the prompt's stated order.
8. Canonicalize URLs to `https://jobs.ashbyhq.com/{board}/{uuid}`. Remove
   trailing slashes, `/application`, `/apply`, query parameters, and fragments.
9. Sort newest to oldest and save a new JSON file with exactly:

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

10. Run `scripts/Test-AshbyJobSearch.ps1 -VerifyLive` against the result and
   confirm only the intended new artifact changed.

## Evidence rules

- Omit uncertain candidates; do not use indexed crawl dates as posting dates.
- Treat the current public board response as the source of truth for identity,
  liveness, title, date, location, and content.
- Keep search artifacts separate from operational application status fields.
- Respect repository ignore rules for private queue data. Do not force-add an
  ignored artifact unless the user explicitly requests public synchronization.
