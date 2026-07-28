# Security and privacy

This project processes identity, contact, resume, employment, and application data. Operate it only for a candidate account and target applications you are explicitly authorized to use.

## Protect local secrets

- Keep Google OAuth credentials, OAuth tokens, Vertex service-account keys, candidate profile data, email pools, resume sources, and generated output out of Git.
- Use the provided example JSON files as templates; do not put real values in examples, test fixtures, issues, or commit messages.
- Limit local filesystem access and remove unused credential copies. Revoke and rotate a credential if it is exposed.
- Prefer `--redact` when exporting Gmail data. Review every export before sharing it.

## Submission and email safety

- Start application workflows with `--dry-run` or `--fill-only`.
- Use `--live-submit` only after a candidate review. Do not infer an answer to sensitive, legal, work-authorization, compensation, or screening questions.
- A browser fill is not a submission. Depend on explicit confirmation and `submission_log.json`.
- Gmail sends require confirmation unless `--yes` is supplied. Use `--draft` for reviewable outreach.
- Do not use queue processing for exploratory testing; it is a live submission workflow.

## Retention and sharing

Generated artifacts, result JSON, screenshots, and logs can contain personal data. Keep only what is needed for the candidate's application process, share it only with authorized people, and securely delete local copies according to the candidate's retention requirements.
