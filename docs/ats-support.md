# ATS Support

This document describes the supported Applicant Tracking System (ATS) providers, their capabilities, and diagnostic procedures. For adding support for new providers, see [Architecture](architecture.md#extension-points). For troubleshooting provider-specific issues, see [Troubleshooting Guide](troubleshooting.md).


The application orchestrator supports five ATS providers. Search has first-class public-board discovery for Ashby, Greenhouse, Lever, SmartRecruiters, and Workable. Generic `web` discovery can still find other providers, but only a job-specific URL for one of the five supported providers is eligible for automated application.

For the original feasibility analysis and remaining-provider roadmap, see [ATS Automation Feasibility & RICE Analysis](ATS_AUTOMATION_FEASIBILITY_AND_RICE.md).


| Provider | Search | Application | Notes |
| --- | --- | --- | --- |
| Ashby | Public board/feed and page discovery | Supported | Ashby URLs can provide context for resume and cover-letter generation. |
| Greenhouse | Public board/feed and page discovery | Supported | Use headed diagnostic runs when a provider flow needs inspection. |
| Lever | Public board/feed and page discovery | Supported | Search pagination is controlled by `max_lever_pages`. |
| Workable | Public account feed and page discovery | Guarded browser form | Supports standard identity, résumé, cover-letter, required-field, CAPTCHA, and confirmation gates. |
| SmartRecruiters | Public posting feed and page discovery | Guarded browser form | Stops safely when the OneClick flow presents anti-bot verification. |
| Recruitee | Generic JSON-LD (`web`) | Not supported | Discovery results are not routed to an application engine. |
| BambooHR | Generic JSON-LD (`web`) | Not supported | Discovery results are not routed to an application engine. |
| Breezy HR | Not configured | Not supported | No application engine is registered. |
| JazzHR | Not configured | Not supported | No application engine is registered. |
| Other generic JSON-LD (`web`) | Page parsing | Not supported | Discovery-only when no supported ATS job URL can be inferred. |

Provider pages change frequently. A supported provider means the engine has automated handling for known patterns, not that every custom question, anti-bot check, or employer-specific policy can be completed. The engines report `REQUIRED_FIELDS_NOT_FILLED`, `CAPTCHA_REQUIRED`, `SUBMIT_BUTTON_NOT_FOUND`, `CONFIRMATION_PRESENT_BEFORE_SUBMIT`, or `SUBMISSION_UNCONFIRMED` rather than treating an incomplete or potentially duplicate attempt as success. Only exact `SUBMITTED & CONFIRMED` results count.

## Diagnostic runs

Provider adapters require an orchestrator invocation. Use the normal `apply` command with a job-specific URL for authorized investigation; `--fill-only --headed` opens and fills the form without clicking Submit:

```powershell
python src/job_automation.py apply --url "https://…" --fill-only --headed
```

This path detects the provider, selects the configured candidate email, creates URL-specific material, and records results consistently.

## Reporting a compatibility gap

Record the provider, sanitized URL, mode used, current browser visibility, result status, and a redacted diagnostic log excerpt. Application screenshots are deleted automatically after every terminal attempt. Never attach OAuth tokens, candidate profile content, resume source data, or an unredacted application page to an issue.

---

**See Also:**
- [Architecture](architecture.md#extension-points) - How to add new ATS providers
- [Operations Runbook](operations-runbook.md) - Operating procedures
- [CLI Reference](cli-reference.md) - Command documentation
- [FAQ](faq.md#what-providers-are-supported) - Provider FAQ
