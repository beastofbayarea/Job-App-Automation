# Operations runbook

## Safe operating sequence

1. Update the candidate profile, email pool, and approved resume source.
2. Search public boards and inspect `output/ai_jobs.csv` plus `output/job_search_coverage.json`.
3. Generate and review a tailored resume or cover letter.
4. Run `apply` with `--dry-run` or `--fill-only` first.
5. Review browser evidence and the orchestration result before using `--live-submit`.
6. Confirm that a live submission is recorded in `output/submission_log.json`.

`apply` is dry-run by default. A filled form is not a confirmed submission. The queue command is different: it always requests live submission and must be used only for intentional, reviewed queues.

## Search and review

```powershell
python src/job_automation.py search --role-type "Product Manager" --ats-platform greenhouse --verify-live --require-live
```

Use `--board-url`, `--boards-file`, `--career-page`, or `--career-pages-file` for known sources. Use explicit `--posted-on`, `--posted-since`, and `--posted-until` filters when a calendar window matters. Read the coverage report before treating a low result count as absence of roles.

## Application workflow

```powershell
# Safe workflow check for one known job.
python src/job_automation.py apply --url "https://…" --company "Example" --role "Product Manager" --dry-run

# Drive the form without submitting, then inspect the result and screenshots.
python src/job_automation.py apply --url "https://…" --company "Example" --role "Product Manager" --fill-only --headed
```

For a reviewed live run, replace the mode with `--live-submit`. Use `--results-file` and `--submission-log-file` to retain separate evidence for a campaign. URL mode ignores `--tracker`; tracker mode uses the configured spreadsheet and accepts `--limit`, `--start-index`, and `--no-shuffle`.

## Queue recovery

The queue contains one non-empty URL per line. It stops on the first unconfirmed result and writes `output/job_url_queue_progress.json`, including `last_index`, `last_url`, confirmation state, and the engine result.

Do not blindly rerun a failed queue. First inspect the provider page, generated result, and submission log. When the previous entry was not submitted, resume with the next intended zero-based index:

```powershell
python src/job_automation.py queue --queue .\jobs.txt --start-index 3
```

If submission status is uncertain, verify it with the employer account or confirmation email before retrying. This avoids duplicate applications.

## Operational artifacts

- `output/orchestration_results.json`: result records from an application run.
- `output/submission_log.json`: only confirmed submissions recorded by the orchestrator.
- `output/job_url_queue_progress.json`: latest queue checkpoint.
- `output/ai_jobs.csv` and `output/job_search_coverage.json`: search results and coverage evidence.

All persisted artifacts use an atomic replace, so a completed write is not partially visible.
