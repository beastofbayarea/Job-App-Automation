# Configuration

This guide covers all configuration options for the Job Application Automation toolkit. For quick setup, see [Quick Reference](quick-reference.md#configuration-quick-reference). For troubleshooting configuration issues, see [Troubleshooting Guide](troubleshooting.md#configuration-file-missing-or-invalid).

Copy the tracked examples before adding personal data. Local candidate data, credentials, OAuth tokens, generated files, and caches are intentionally Git-ignored.

# Configuration

Copy the tracked examples before adding personal data. Local candidate data, credentials, OAuth tokens, generated files, and caches are intentionally Git-ignored.

| Need | Local path | Source |
| --- | --- | --- |
| Application answers and browser policy | `config/candidate_profile_config.json` | `config/candidate_profile_config.example.json` |
| Candidate email addresses | `config/candidate_email_pool.json` | `config/candidate_email_pool.example.json` |
| Resume source material | `data/base_resume.txt` | Create from candidate-approved material |
| Runtime settings | `config/runtime/*.json` | Tracked configuration split by domain |
| Vertex service account | `config/vertex_service_account.json` | `config/vertex_service_account.example.json` |
| Gmail OAuth client and token | `config/credentials.json`, `config/token.json` | Google Cloud OAuth desktop-client credentials; token is created during authorization |
| Private VPS document archive | `config/vps_config.json` | `config/vps_config.example.json` |
| Google site/indexing settings | `config/seo_config.json` | `config/seo_config.example.json` |
| Google cloud roles and Cent Capital reference inventory | `config/cent_capital_config.json` | `config/cent_capital_config.example.json` |

## Candidate profile

`candidate_profile_config.json` provides identity data, approved answers, browser settings, email-pool location, and answer matchers. Treat every value as candidate-approved source data. Do not invent answers for questions that are not covered by the profile; review the application instead.

The `candidate` object supplies personal fields and education history. `policies.answers` holds reusable answers; `policies.eeo` and the demographic fields should remain accurate or use the candidate's preferred disclosure option. `headless_overrides` permits a provider-specific browser choice, while `navigation_timeout_ms`, `action_timeout_ms`, and `attempts` control browser resilience.

List every country where the candidate may legally work in
`policies.answers.work_authorization_countries`. Set `target_work_country` in a
job-specific profile when the form uses ambiguous phrases such as “the country
where this job is located.” Country-specific authorization and residence
questions fail closed when the target cannot be resolved; the global legacy
answers are used only when the country list is absent.

## Runtime configuration

`config/runtime/` contains the shared operational defaults. Each domain is kept
in its own JSON file, while `schema_version.json` versions the complete set:

- `application`: tracker, resume source, output artifact locations, email pool,
  application/queue timeouts, bounded VPS document work
  (`vps_max_document_jobs` and `vps_document_retry_jobs`), and the guarded
  per-ATS application limit (`vps_max_attempts_per_ats`).
- `browser`: the optional Chromium CDP endpoint.
- `vertex`: project, service-account path, model, retry, and job-text limits. `project_id: "from-service-account"` reads the project from the service-account file. Application Default Credentials can instead be supplied through `GOOGLE_APPLICATION_CREDENTIALS`.
- `resume` and `cover_letter`: caches, retry limits, quality threshold, and word limits.
- `search`: default AI/location vocabulary, role and location aliases, ATS discovery
  hosts, liveness markers, provider backends, output paths, and CLI/network defaults.
  Configured modes and backend names must match the supported search CLI values;
  command-line arguments still override these defaults for an individual run.
- `ashby`: navigation and form limits plus confirmation and failure phrases. Older
  external schema-one files may still contain the former Ashby worker pacing keys,
  but new configuration belongs in `continuous_worker` and conflicting duplicate
  values are rejected.
- `gmail`: OAuth credential/token locations and verification polling settings.
- `observability`: non-secret telemetry environment and flush-timeout defaults.
  Sentry credentials and release metadata remain external environment settings.
- `continuous_worker`: direct-worker defaults, source-worker defaults, and sparse
  provider pacing overrides. Provider overrides are validated after inheritance so
  an incomplete minimum/maximum pair cannot defer an invalid state until runtime.

Every section file must contain exactly one top-level object matching its file
name. The loader rejects missing or unexpected JSON files so partial deployments
cannot silently mix old and new defaults. Paths are resolved from the project
root. Keep secrets in the local files named above; never put them in runtime
configuration committed to Git.

## Optional operational telemetry

Install the `observability` package extra and set `SENTRY_DSN` only in the
worker's external environment when centralized worker diagnostics are wanted.
`SENTRY_ENVIRONMENT` and `SENTRY_RELEASE` are optional bounded labels. These
secret or deployment-specific values do not belong in `config/runtime/`; on the
VPS they belong in the root-readable
`/etc/job-application-automation/observability.env` file.
Without `SENTRY_DSN`, the application does not import or initialize the Sentry
SDK.

## Google submission and Cent Capital reference inventory

`config/cent_capital_config.json` is an ignored reference inventory for settings
that belong to the separate Cent Capital application and may be useful in later
work. The `google-indexing` command reads only its Google project,
`search_console_indexing` service-account role, key reference, Indexing API
endpoint/scopes, and quota fields. The remaining inventory groups site/contact
metadata, social links, Contentful profiles, frontend route and sitemap policy,
RSS/IndexNow settings, deployment notes, and other service credentials.

Raw Google key exports matching `config/cent-capital-*-*.json` and the imported
frontend snapshot at `config/config.js` are also ignored. Keep their exact paths
in `reference_sources` and `google.service_accounts` so future work can identify
the Search Console/Indexing and Gemini accounts without treating either key as
the Job App Automation Vertex credential. When promoting these settings into the
owning Cent Capital repository, resolve paths relative to that repository and
use its environment/secret-loading conventions.

`config/seo_config.json` owns the settings for this repository's published site:

- `domain` and `gsc.sitemap_url` define the owned HTTPS site and sitemap.
- `google_submission.cloud_config_file` links to the ignored cloud inventory.
- `google_submission.search_console_property` must be the matching
  `sc-domain:<domain>` property.
- `google_submission.eligible_urls` contains only owned pages eligible for the
  direct Google Indexing API. General pages remain in `indexed_urls` and are
  covered through the sitemap.
- `request_timeout_seconds` bounds site and Google API calls, while `report_file`
  stores the atomic submission/status report.

The loader rejects mismatched key IDs, account emails, project IDs, foreign
hosts, untrusted endpoints, duplicate URLs, excessive batches, and missing
Indexing API scope before authentication. The two Google uses are distinct:
Search Console sitemap submission is appropriate for the entire site, while
direct notifications are accepted only for `JobPosting` pages or qualifying
livestream `BroadcastEvent` pages.

## Private VPS document archive

Copy `config/vps_config.example.json` to the ignored `config/vps_config.json`. The `documents` workflow reads these fields from its `vps` object:

- `host`, `ssh_user`, and optional `ssh_port`: use a dedicated, unprivileged archive account.
- `ssh_host_key`: the trusted PuTTY-format fingerprint obtained through an independent channel. Archive operations require it and pass it through `-hostkey`.
- `document_archive_root`: a private absolute POSIX path outside the repository and every web root. The default is `/var/lib/job-application-automation/private-archive`.
- `archive_private_key_file`: a dedicated PuTTY key file. This is preferred and must not be the Git publication deploy key.
- `ssh_password.value`: compatibility fallback when no archive key is configured. It is supplied to PuTTY through a per-run `-pwfile`, never a process argument.

The private key, password, and real config remain ignored by Git. The runtime configuration schema is unchanged.

## Minimal setup check

```powershell
Copy-Item config\candidate_profile_config.example.json config\candidate_profile_config.json
Copy-Item config\candidate_email_pool.example.json config\candidate_email_pool.json
# Create data\base_resume.txt from approved resume content.
python src/job_automation.py email-pool --count 1
```

The email-pool command verifies only that the local pool can be read. It does not validate a mailbox.

---

**See Also:**
- [Quick Reference](quick-reference.md#configuration-quick-reference) - Configuration examples
- [Operations Runbook](operations-runbook.md) - Operating procedures
- [Security & Privacy](security-and-privacy.md) - Data protection guidelines
- [FAQ](faq.md#configuration) - Configuration FAQ
