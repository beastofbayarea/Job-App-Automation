# Configuration

Copy the tracked examples before adding personal data. Local candidate data, credentials, OAuth tokens, generated files, and caches are intentionally Git-ignored.

| Need | Local path | Source |
| --- | --- | --- |
| Application answers and browser policy | `config/candidate_profile_config.json` | `config/candidate_profile_config.example.json` |
| Candidate email addresses | `config/candidate_email_pool.json` | `config/candidate_email_pool.example.json` |
| Resume source material | `data/base_resume.txt` | Create from candidate-approved material |
| Runtime settings | `config/runtime_config.json` | Tracked default configuration |
| Vertex service account | `config/vertex_service_account.json` | `config/vertex_service_account.example.json` |
| Gmail OAuth client and token | `config/credentials.json`, `config/token.json` | Google Cloud OAuth desktop-client credentials; token is created during authorization |
| Private VPS document archive | `config/vps_config.json` | `config/vps_config.example.json` |
| Google site/indexing settings | `config/seo_config.json` | `config/seo_config.example.json` |
| Google cloud roles and Cent Capital reference inventory | `config/cent_capital_config.json` | `config/cent_capital_config.example.json` |

## Candidate profile

`candidate_profile_config.json` provides identity data, approved answers, browser settings, email-pool location, and answer matchers. Treat every value as candidate-approved source data. Do not invent answers for questions that are not covered by the profile; review the application instead.

The `candidate` object supplies personal fields and education history. `policies.answers` holds reusable answers; `policies.eeo` and the demographic fields should remain accurate or use the candidate's preferred disclosure option. `headless_overrides` permits a provider-specific browser choice, while `navigation_timeout_ms`, `action_timeout_ms`, and `attempts` control browser resilience.

## Runtime configuration

`config/runtime_config.json` is the shared default for operational paths and limits. Its main sections are:

- `application`: tracker, resume source, output artifact locations, email pool,
  application/queue timeouts, bounded VPS document work
  (`vps_max_document_jobs` and `vps_document_retry_jobs`), and the guarded
  per-ATS application limit (`vps_max_attempts_per_ats`).
- `browser`: the optional Chromium CDP endpoint.
- `vertex`: project, service-account path, model, retry, and job-text limits. `project_id: "from-service-account"` reads the project from the service-account file. Application Default Credentials can instead be supplied through `GOOGLE_APPLICATION_CREDENTIALS`.
- `resume` and `cover_letter`: caches, retry limits, quality threshold, and word limits.
- `ashby`: navigation and form limits plus confirmation and failure phrases.
- `gmail`: OAuth credential/token locations and verification polling settings.

Paths are resolved from the project root. Keep secrets in the local files named above; never put them in runtime configuration committed to Git.

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
