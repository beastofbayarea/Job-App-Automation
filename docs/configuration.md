# Configuration

This guide covers all configuration options for the Job Application Automation toolkit. For quick setup, see [Quick Reference](quick-reference.md#configuration-quick-reference). For troubleshooting configuration issues, see [Troubleshooting Guide](troubleshooting.md#configuration-file-missing-or-invalid).

The repository keeps five active configuration files. Credentials and OAuth
tokens should be supplied at the paths selected by runtime configuration or by
the relevant environment variables.

| Need | Local path | Source |
| --- | --- | --- |
| Application answers and browser policy | `config/candidate_profile_config.json` | Review before live use |
| Candidate email addresses | `config/candidate_email_pool.json` | Review before live use |
| Resume source material | `data/base_resume.txt` | Create from candidate-approved material |
| Runtime settings | `config/runtime_config.json` | Single tracked operational configuration |
| Vertex service account | Path in `config/runtime_config.json` | Supply outside Git |
| Gmail OAuth client and token | `config/credentials.json`, `config/token.json` | Google Cloud OAuth desktop-client credentials; token is created during authorization |
| Private VPS document archive | `config/vps_config.json` | Review before live use |
| Google site/indexing settings | `config/seo_config.json` | Pass private cloud configuration separately |

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

`config/runtime_config.json` contains the shared operational defaults. Its
`schema_version` versions the complete document:

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

The loader validates every section before use. Paths are resolved from the
project root. Keep secrets outside runtime configuration.

## Optional operational telemetry

Install the `observability` package extra and set `SENTRY_DSN` only in the
worker's external environment when centralized worker diagnostics are wanted.
`SENTRY_ENVIRONMENT` and `SENTRY_RELEASE` are optional bounded labels. These
secret or deployment-specific values do not belong in `config/runtime_config.json`; on the
VPS they belong in the root-readable
`/etc/job-application-automation/observability.env` file.
Without `SENTRY_DSN`, the application does not import or initialize the Sentry
SDK.

## Google submission

`config/seo_config.json` owns the settings for this repository's published site:

- `domain` and `gsc.sitemap_url` define the owned HTTPS site and sitemap.
- `google_submission.cloud_config_file` links to private cloud configuration
  supplied outside the tracked config set.
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

The `documents` workflow reads these fields from `config/vps_config.json`:

- `host`, `ssh_user`, and optional `ssh_port`: use a dedicated, unprivileged archive account.
- `ssh_host_key`: the trusted PuTTY-format fingerprint obtained through an independent channel. Archive operations require it and pass it through `-hostkey`.
- `document_archive_root`: a private absolute POSIX path outside the repository and every web root. The default is `/var/lib/job-application-automation/private-archive`.
- `archive_private_key_file`: a dedicated PuTTY key file. This is preferred and must not be the Git publication deploy key.
- `ssh_password.value`: compatibility fallback when no archive key is configured. It is supplied to PuTTY through a per-run `-pwfile`, never a process argument.

The private key, password, and real config remain ignored by Git. The runtime configuration schema is unchanged.

## Minimal setup check

```powershell
# Review the candidate profile and email pool, then check the pool.
python src/job_automation.py email-pool --count 1
```

The email-pool command verifies only that the local pool can be read. It does not validate a mailbox.

---

**See Also:**
- [Quick Reference](quick-reference.md#configuration-quick-reference) - Configuration examples
- [Operations Runbook](operations-runbook.md) - Operating procedures
- [Security & Privacy](security-and-privacy.md) - Data protection guidelines
- [FAQ](faq.md#configuration) - Configuration FAQ
