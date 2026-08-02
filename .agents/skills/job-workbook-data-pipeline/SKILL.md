---
name: job-workbook-data-pipeline
description: Managing job application tracking workbooks (Excel/CSV) using openpyxl and exceljs, deduplicating URLs, and running integrity checks.
---

# Job Workbook Data Pipeline Skill

This skill provides patterns for processing private job URL workbooks and managing application records.

## Data Pipeline Rules

1. **URL Cleaning & Deduplication**:
   - Strip tracking parameters (`utm_source`, `ref`, `gh_src`, etc.) from job links for canonical comparison.
   - Flag duplicate postings across workbooks and database archives.

2. **Excel Workbooks (Node & Python)**:
   - Use `exceljs` for Node scripts (`scripts/cleanup_job_url_workbooks.mjs`).
   - Use `openpyxl` for Python processing in `src/job_application_automation/`.

3. **Status Life Cycle**:
   - Workflows track states: `DISCOVERED` -> `CANDIDATE` -> `SUBMITTING` -> `APPLIED` / `FAILED`.
   - Log submission timestamps and confirmation IDs securely.
