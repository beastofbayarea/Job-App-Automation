# Security and Privacy

This document outlines the security and privacy considerations for operating the Job Application Automation toolkit. **Read this before deploying or using the system with real candidate data.** For safe operating procedures, see [Operations Runbook](operations-runbook.md). For troubleshooting security issues, see [Troubleshooting Guide](troubleshooting.md).


This project processes identity, contact, resume, employment, and application data. Operate it only for a candidate account and target applications you are explicitly authorized to use.

## Protect local secrets

- Keep Google OAuth credentials, OAuth tokens, Vertex/Search Console/Indexing service-account keys, candidate profile data, email pools, resume sources, and generated output out of Git.
- Keep the VPS archive config, private key, PDFs, manifests, private
  document/application job metadata, and candidate emails out of Git and every
  publicly served directory.
- Use the provided example JSON files as templates; do not put real values in examples, test fixtures, issues, or commit messages.
- Limit local filesystem access and remove unused credential copies. Revoke and rotate a credential if it is exposed.
- Prefer `--redact` when exporting Gmail data. Review every export before sharing it.

## Submission and email safety

- Start interactive application workflows with `--dry-run` or `--fill-only`.
- Use `--live-submit` only after a candidate review. Do not infer an answer to sensitive, legal, work-authorization, compensation, or screening questions.
- A browser fill is not a submission. Depend on explicit confirmation and `submission_log.json`.
- Gmail sends require confirmation unless `--yes` is supplied. Use `--draft` for reviewable outreach.
- Do not use queue processing for exploratory testing; it is a live submission workflow.
- VPS ATS workers are explicitly authorized for unattended live submission.
  Failed applications are recorded and skipped while later roles continue; do
  not weaken the no-automatic-retry rule or exact confirmation requirement.

## Retention and sharing

Generated artifacts, result JSON, screenshots, and logs can contain personal data. Screenshots are temporary and are deleted automatically after each application attempt. Keep only what is needed for the candidate's application process, share it only with authorized people, and securely delete local copies according to the candidate's retention requirements.

Optional Sentry telemetry is disabled by default and uses no logging,
breadcrumb, tracing, profiling, stack-trace, source-context, local-variable,
request, or user integrations. When explicitly enabled, it sends only a fixed
event name and these allow-listed operational tags: worker kind, validated
worker ID, ATS provider, stage, cycle status, exception class name, numeric exit
code, timeout flag, and failure count. It never sends job URLs or digests,
company or role names, email addresses, candidate data, form values, document
contents, filesystem paths, stdout/stderr, exception messages, or attachments.
The final `before_send` filter drops all non-allow-listed event content.

## VPS archive boundary

- Use a dedicated unprivileged SSH account, archive root mode `0700`, file mode `0600`, and a separately managed authentication key.
- Pin the expected SSH host key. Never accept an unexpected key merely to make a transfer work.
- VPS application state, results, temporary screenshots, submission logs, Gmail OAuth
  files, and candidate inputs are private and must never enter a public worktree.
- Pull confirmed submissions and failure reports only with
  `pull_vps_application_reports.ps1`; its local destination remains ignored by
  Git. Review and securely remove downloaded copies when no longer needed.
- Archive records are immutable. A content conflict requires review; it is never silently overwritten.
- Retrieval validates the supplied identity and every document hash before replacing local files.
- Maintain encrypted backups appropriate for the candidate's retention policy. Private permissions alone do not protect against disk loss or account compromise.

---

**See Also:**
- [Operations Runbook](operations-runbook.md) - Safe operating procedures
- [Configuration Guide](configuration.md) - Configuration options
- [FAQ](faq.md#is-this-safe-to-use) - Security FAQ
- [Troubleshooting Guide](troubleshooting.md) - Issue resolution
