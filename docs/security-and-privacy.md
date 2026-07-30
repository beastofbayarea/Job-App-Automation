# Security and privacy

This project processes identity, contact, resume, employment, and application data. Operate it only for a candidate account and target applications you are explicitly authorized to use.

## Protect local secrets

- Keep Google OAuth credentials, OAuth tokens, Vertex/Search Console/Indexing service-account keys, candidate profile data, email pools, resume sources, and generated output out of Git.
- Keep the VPS archive config, private key, PDFs, manifests, job metadata, and candidate emails out of Git and every publicly served directory.
- Use the provided example JSON files as templates; do not put real values in examples, test fixtures, issues, or commit messages.
- Limit local filesystem access and remove unused credential copies. Revoke and rotate a credential if it is exposed.
- Prefer `--redact` when exporting Gmail data. Review every export before sharing it.

## Submission and email safety

- Start interactive application workflows with `--dry-run` or `--fill-only`.
- Use `--live-submit` only after a candidate review. Do not infer an answer to sensitive, legal, work-authorization, compensation, or screening questions.
- A browser fill is not a submission. Depend on explicit confirmation and `submission_log.json`.
- Gmail sends require confirmation unless `--yes` is supplied. Use `--draft` for reviewable outreach.
- Do not use queue processing for exploratory testing; it is a live submission workflow.
- The VPS daily workflow is explicitly authorized for unattended live
  submission. It is capped at 10 attempts per ATS. Failed applications are
  recorded and skipped while later roles continue; do not weaken the
  no-automatic-retry rule or exact confirmation requirement.

## Retention and sharing

Generated artifacts, result JSON, screenshots, and logs can contain personal data. Keep only what is needed for the candidate's application process, share it only with authorized people, and securely delete local copies according to the candidate's retention requirements.

## VPS archive boundary

- Use a dedicated unprivileged SSH account, archive root mode `0700`, file mode `0600`, and a separately managed authentication key.
- Pin the expected SSH host key. Never accept an unexpected key merely to make a transfer work.
- Do not reuse the Git search-publication deploy key for private documents.
- `vps-search-output` is public generated data and must remain limited to search coverage, job CSV, and board-cache artifacts. Never add archive paths or content to its allow-list.
- VPS application state, results, screenshots, submission logs, Gmail OAuth
  files, and candidate inputs are private and must never enter the search
  publication worktree.
- Pull confirmed submissions and failure reports only with
  `pull_vps_application_reports.ps1`; its local destination remains ignored by
  Git. Review and securely remove downloaded copies when no longer needed.
- Archive records are immutable. A content conflict requires review; it is never silently overwritten.
- Retrieval validates the supplied identity and every document hash before replacing local files.
- Maintain encrypted backups appropriate for the candidate's retention policy. Private permissions alone do not protect against disk loss or account compromise.
