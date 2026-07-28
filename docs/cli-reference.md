# CLI reference

Run commands with either `python src/job_automation.py` from the repository root or the installed `job-automation` command. Use `<command> --help` for the version-specific argparse help.

| Command | Purpose | Required inputs |
| --- | --- | --- |
| `search` | Discover public ATS vacancies | `--role-type`, `--ats-platform` |
| `resume` | Generate a tailored PDF resume | `--company`, `--role` |
| `cover-letter` | Generate a one-page cover letter | `--company`, `--role` |
| `documents generate` | Generate a matched CV/cover-letter pair | URL, company, role, email, JD context |
| `documents store` | Plan or execute a private VPS upload | URL, company, role, email, both PDFs |
| `documents retrieve` | Retrieve and verify both archived PDFs | URL, company, role, email |
| `apply` | Orchestrate an ATS application | `--url` or configured tracker |
| `queue` | Sequential live submissions | `--queue` |
| `gmail` | Read, export, draft, or send Gmail | Local OAuth credentials |
| `email-pool` | Select configured candidate email addresses | Email pool file |
| `engine <provider>` | Direct ATS diagnostic | Provider-specific URL and resume |

## Safety modes

`apply` accepts at most one of `--live-submit`, `--fill-only`, and `--dry-run`; without an explicit mode it remains dry-run. `queue` deliberately invokes `apply --live-submit` for each URL and stops after an unconfirmed result.

## Common options

- `search`: repeat `--role-type`, `--ats-platform`, `--location`, and source URL options as needed. `--require-live` implies `--verify-live`. Results default to `output/ai_jobs.csv`; `--json-output` is optional.
- `resume`: accept job text through `--keywords`, `--jd-overview`, `--jd-resp`, and `--jd-req`, or use `--url` for Ashby context fallback. `--email` changes only the generated document.
- `cover-letter`: accepts the same segmented job text, `--jd-file`, `--profile`, `--output`, and an `--email` override for the rendered contact header.
- `documents generate`: accepts `--jd-file` or segmented `--jd-*` text. It writes both PDFs under an opaque application ID. `--archive` is the explicit live VPS action.
- `documents store`: validates PDF type, size, identity, and hashes locally. It is an offline plan unless `--execute` is supplied.
- `documents retrieve`: uses the URL and normalized email to resolve an opaque ID, then requires the company and title to match the immutable manifest. It downloads both files to a temporary directory and promotes them only after SHA-256 verification. Existing archive files are preserved unless `--overwrite` is explicit.
- `apply`: use `--tracker`, `--resume`, `--config`, `--results-file`, `--submission-log-file`, `--limit`, `--start-index`, `--timeout`, `--resume-timeout`, and `--headed` to control a run.
- `queue`: `--start-index` is zero-based; `--timeout` applies per application.
- `gmail`: use `--query`, `--unread`, `--all-mail`, `--include-body`, and `--classify` for reading. `--csv`/`--json` export results and `--redact` masks sensitive fields. `--draft` creates a draft; `--yes` bypasses the send confirmation prompt.

## Exit status

Commands generally return `0` on success, `1` for an unsuccessful workflow or remote archive failure, and `2` for invalid command-line input. Gmail additionally uses `3` for a configuration error, `4` for an OAuth/API error, and `130` when interrupted. Treat an application as submitted only when its non-test result reports confirmed submission and the submission log contains it.
