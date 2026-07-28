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

## Candidate profile

`candidate_profile_config.json` provides identity data, approved answers, browser settings, email-pool location, and answer matchers. Treat every value as candidate-approved source data. Do not invent answers for questions that are not covered by the profile; review the application instead.

The `candidate` object supplies personal fields and education history. `policies.answers` holds reusable answers; `policies.eeo` and the demographic fields should remain accurate or use the candidate's preferred disclosure option. `headless_overrides` permits a provider-specific browser choice, while `navigation_timeout_ms`, `action_timeout_ms`, and `attempts` control browser resilience.

## Runtime configuration

`config/runtime_config.json` is the shared default for operational paths and limits. Its main sections are:

- `application`: tracker, resume source, output artifact locations, email pool, and application/queue timeouts.
- `browser`: the optional Chromium CDP endpoint.
- `vertex`: project, service-account path, model, retry, and job-text limits. `project_id: "from-service-account"` reads the project from the service-account file. Application Default Credentials can instead be supplied through `GOOGLE_APPLICATION_CREDENTIALS`.
- `resume` and `cover_letter`: caches, retry limits, quality threshold, and word limits.
- `ashby`: navigation and form limits plus confirmation and failure phrases.
- `gmail`: OAuth credential/token locations and verification polling settings.

Paths are resolved from the project root. Keep secrets in the local files named above; never put them in runtime configuration committed to Git.

## Minimal setup check

```powershell
Copy-Item config\candidate_profile_config.example.json config\candidate_profile_config.json
Copy-Item config\candidate_email_pool.example.json config\candidate_email_pool.json
# Create data\base_resume.txt from approved resume content.
python src/job_automation.py email-pool --count 1
```

The email-pool command verifies only that the local pool can be read. It does not validate a mailbox.
