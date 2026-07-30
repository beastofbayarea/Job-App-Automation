# ATS support

The application orchestrator supports nine ATS providers. Search has native public-board discovery for Ashby, Greenhouse, and Lever; the six phase-one providers are discovered through `web` JSON-LD pages and routed to their engine from a job-specific `job_url` or `apply_url`.

For the original feasibility analysis and remaining-provider roadmap, see [ATS Automation Feasibility & RICE Analysis](ATS_AUTOMATION_FEASIBILITY_AND_RICE.md).


| Provider | Search | Application | Notes |
| --- | --- | --- | --- |
| Ashby | Public board/feed and page discovery | Supported | Ashby URLs can provide context for resume and cover-letter generation. |
| Greenhouse | Public board/feed and page discovery | Supported | Use headed diagnostic runs when a provider flow needs inspection. |
| Lever | Public board/feed and page discovery | Supported | Search pagination is controlled by `max_lever_pages`. |
| Workable | Generic JSON-LD (`web`) | Guarded browser form | Supports standard identity, résumé, cover-letter, required-field, CAPTCHA, and confirmation gates. |
| SmartRecruiters | Generic JSON-LD (`web`) | Guarded browser form | Stops safely when the OneClick flow presents anti-bot verification. |
| Recruitee | Generic JSON-LD (`web`) | Guarded browser form | Supports current dotted candidate field names and file controls. |
| BambooHR | Generic JSON-LD (`web`) | Guarded browser form | Provider pages vary; use fill-only diagnostics before a live attempt. |
| Breezy HR | Generic JSON-LD (`web`) | Guarded browser form | Supports current `cName`, `cEmail`, `cResume`, and cover-letter controls. |
| JazzHR | Generic JSON-LD (`web`) | Guarded browser form | Supports current `resumator-*` fields and anchor-based submit control. |
| Other generic JSON-LD (`web`) | Page parsing | Not supported | Discovery-only when no supported ATS job URL can be inferred. |

Provider pages change frequently. A supported provider means the engine has automated handling for known patterns, not that every custom question, anti-bot check, or employer-specific policy can be completed. The engines report `REQUIRED_FIELDS_NOT_FILLED`, `CAPTCHA_REQUIRED`, `SUBMIT_BUTTON_NOT_FOUND`, `CONFIRMATION_PRESENT_BEFORE_SUBMIT`, or `SUBMISSION_UNCONFIRMED` rather than treating an incomplete or potentially duplicate attempt as success. Only exact `SUBMITTED & CONFIRMED` results count.

## Diagnostic runs

Provider adapters require an orchestrator invocation. Use the normal `apply` command with a job-specific URL for authorized investigation; `--fill-only --headed` opens and fills the form without clicking Submit:

```powershell
python src/job_automation.py apply --url "https://…" --fill-only --headed
```

This path detects the provider, selects the configured candidate email, creates URL-specific material, and records results consistently.

## Reporting a compatibility gap

Record the provider, sanitized URL, mode used, current browser visibility, result status, and a redacted screenshot. Never attach OAuth tokens, candidate profile content, resume source data, or an unredacted application page to an issue.
