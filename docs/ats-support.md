# ATS support

The application orchestrator supports Ashby, Greenhouse, and Lever. Search also supports their public board feeds plus `web` for generic JSON-LD job pages.

| Provider | Search | Application | Notes |
| --- | --- | --- | --- |
| Ashby | Public board/feed and page discovery | Supported | Ashby URLs can provide context for resume and cover-letter generation. |
| Greenhouse | Public board/feed and page discovery | Supported | Use headed diagnostic runs when a provider flow needs inspection. |
| Lever | Public board/feed and page discovery | Supported | Search pagination is controlled by `max_lever_pages`. |
| Generic JSON-LD (`web`) | Page parsing | Not supported | Discovery-only; it is not an application engine. |

Provider pages change frequently. A supported provider means the engine has automated handling for known patterns, not that every custom question, anti-bot check, or employer-specific policy can be completed safely.

## Diagnostic runs

Use a direct engine command only for authorized investigation. It requires a URL and resume and accepts the same `--dry-run`, `--fill-only`, `--live-submit`, and `--headed` controls as an engine run:

```powershell
python src/job_automation.py engine greenhouse --url "https://…" --resume ".\resume.pdf" --dry-run --headed
```

Prefer `apply` for normal work because it detects the provider, selects the configured candidate email, creates URL-specific material, and records results consistently.

## Reporting a compatibility gap

Record the provider, sanitized URL, mode used, current browser visibility, result status, and a redacted screenshot. Never attach OAuth tokens, candidate profile content, resume source data, or an unredacted application page to an issue.
