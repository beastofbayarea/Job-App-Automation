# Product Requirements Document: Future Job-Application Workflow Features

**Product:** Job Application Automation<br>
**Status:** Draft — future implementation roadmap<br>
**Last updated:** 2026-07-28

## 1. Purpose

This document defines five future features that build on the existing ATS search,
resume-generation, Gmail, orchestration, and artifact-persistence workflows:

1. Applied-role registry and duplicate-submission prevention.
2. One-page, fact-grounded personalised cover letters.
3. JD-contact extraction and resume-attached outreach drafts.
4. Gmail SendAs alias audit and setup support.
5. Evidence-based recruiter and hiring-manager discovery.

The product goal is to reduce duplicate work while preserving candidate control,
truthfulness, reviewability, and explicit authorization for any external action.

## 2. Product principles and non-negotiable safety rules

- Never submit an ATS application twice because of a retry, duplicate URL, or
  queue restart.
- Never invent career facts, metrics, employers, dates, skills, or personal
  motivation in generated documents or messages.
- Never send an email without a deliberate send action and confirmation.
- Never treat a guessed or weakly inferred email address as verified.
- Preserve enough provenance to explain why an application was skipped, a
  contact was selected, or a draft was created.
- Keep credentials, OAuth tokens, SMTP credentials, verification codes, and
  unnecessary personal data out of artifacts and version control.
- Preserve the repository's deterministic, socket-free automated-test model.

## 3. Current baseline and reusable code

The proposed features should extend rather than replace the existing structure.

- The `apply` workflow already loads jobs, generates a URL-specific resume,
  invokes an ATS engine, and atomically persists a result after each job.
- `EngineResult.is_confirmed_submission` is the strict existing predicate for a
  successful, confirmed application. It must remain the basis for registry and
  post-submission behavior.
- `artifacts.py` already provides atomic text and JSON writes.
- `resume_source.py` supplies tagged candidate identity, experience, education,
  and claim evidence. `resume_ai_client.py` provides an injectable LLM gateway.
- ReportLab and PyMuPDF are already available for document rendering and PDF
  validation.
- The `gmail` workflow already creates Gmail drafts or sends MIME messages,
  but currently has no attachment or From-alias parameter.
- Gmail OAuth currently requests read-only, send, and compose permissions.
- Search provider adapters retrieve rich job descriptions for Greenhouse,
  Lever, Ashby, and JSON-LD pages, but public search output does not retain the
  full description. A dedicated job-context handoff is therefore needed.
- The configured email pool is only a syntactic list sampler today. The
  orchestrator currently derives an application email from the resume or
  candidate fallback email, so alias selection must be deliberately integrated
  before resume generation.

## 4. Shared foundation

### 4.1 Job identity

Add a pure `job_identity.py` module used by the registry, context service,
contact discovery, and outreach history.

Each job identity must contain:

- A canonical job URL.
- ATS/platform name.
- A provider-scoped job identifier when it can be extracted safely.
- Optional board token and region.
- A schema version so canonicalization can evolve without silently breaking
  duplicate prevention.

Use multiple aliases for one role:

```text
url:https://boards.greenhouse.io/example/jobs/123
provider:greenhouse:global:example:123
```

Canonicalization must normalize host case, fragments, trailing slashes, and
known marketing parameters while preserving real ATS identifiers. If an ATS ID
cannot be established safely, canonical URL is the sole identity.

### 4.2 Job context

Add a provider-aware `job_context.py` service that produces one trusted,
reusable representation of the posting:

```json
{
  "schema_version": 1,
  "canonical_url": "https://...",
  "ats": "greenhouse",
  "provider_job_id": "123",
  "company": "Example",
  "role": "Product Manager",
  "jd_text": "...",
  "jd_sha256": "...",
  "source_url": "https://...",
  "retrieved_at": "2026-07-28T00:00:00Z"
}
```

Context acquisition priority:

1. Explicit `--jd-file` supplied by the user.
2. Exact provider API/detail endpoint for Greenhouse, Lever, or Ashby.
3. JSON-LD `JobPosting` data on the exact job page.
4. Explicit, bounded browser fallback when static/provider retrieval fails.

Do not present a document as personalised if no trustworthy JD context is
available. Fail with `JD_CONTEXT_UNAVAILABLE` instead.

### 4.3 Sender identity

Add a `sender_identity.py` resolver after alias audit exists. It must choose a
ready alias before resume generation and pin that choice to the job/run.

```text
ready sender alias
    -> personalised resume contact email
    -> ATS application email
    -> Gmail From address for future outreach
```

Artifacts should store a masked address or stable fingerprint rather than a
raw address unless the raw value is strictly required locally.

## 5. Feature F1 — Applied-role registry

### 5.1 User story

As a candidate, I want the workflow to skip roles I have already successfully
submitted, so retries and queues cannot accidentally create duplicate
applications.

### 5.2 Functional requirements

- Default behavior skips only a validated, live `SUBMITTED & CONFIRMED` role.
- Failed, timed-out, dry-run, fill-only, unconfirmed, malformed, and legacy
  results remain retryable by default.
- A registry write requires all of the following:
  - live-submit mode;
  - `EngineResult.is_confirmed_submission` is true;
  - `test_mode` is explicitly false.
- The registry lookup occurs before personalised-resume generation and ATS
  engine invocation.
- A registry entry is written immediately after a confirmed result and before
  another queue item begins.
- If a registry write fails after an actual submission, stop the live queue for
  manual reconciliation rather than risk another submission.
- A skipped result is terminal but distinct from a new application:
  `status: SKIPPED_ALREADY_APPLIED`, `success: true`, `submitted: false`, and
  `skipped: true`.
- Queue processing must recognize this terminal skip and continue to the next
  URL rather than treat it as an application failure.

### 5.3 Data model

Recommended initial artifact: `output/application_registry.json`.

```json
{
  "schema_version": 1,
  "entries": {
    "url:https://boards.greenhouse.io/example/jobs/123": {
      "aliases": [
        "url:https://boards.greenhouse.io/example/jobs/123",
        "provider:greenhouse:global:example:123"
      ],
      "company": "Example",
      "role": "Product Manager",
      "status": "SUBMITTED & CONFIRMED",
      "test_mode": false,
      "resume_name": "Example_Product_Manager_abc_Resume.pdf",
      "sender_identity_fingerprint": "sha256:...",
      "confirmed_at": "2026-07-28T00:00:00Z",
      "source_result_file": "output/orchestration_results.json",
      "source_row": 1
    }
  }
}
```

Historical imports must retain `imported_at` and source file modification time;
they must not fabricate an original confirmation timestamp.

### 5.4 CLI and operating options

```powershell
python src/job_automation.py apply --application-registry output/application_registry.json
python src/job_automation.py apply --include-already-applied --dry-run
python src/job_automation.py apply --allow-reapply --reason "posting reopened"
python src/job_automation.py registry import --results output/orchestration_results.json
```

- **Atomic JSON, recommended initially:** aligns with the current sequential
  orchestrator and atomic artifact conventions.
- **Lock-protected JSON:** usable if only a small amount of parallelism is
  introduced.
- **SQLite:** preferred if independent workers may submit or import records
  concurrently, or if richer audit/reporting queries become necessary.
- `--trust-legacy-confirmed` must be an explicit import-only escape hatch and
  mark every trusted legacy record as such.

### 5.5 Acceptance criteria

- Equivalent URLs and trusted provider IDs skip the same confirmed role.
- A failed, dry-run, fill-only, and unconfirmed attempt never creates a skip.
- Registry import is idempotent.
- A registry skip happens before any resume-generation subprocess is started.
- Queue processing continues after a valid registry skip.
- Persistence remains atomic and leaves no partial registry artifact.

## 6. Feature F2 — One-page personalised cover letters

### 6.1 User story

As a candidate, I want a concise, role-specific cover letter that uses my
approved career narrative and verified resume evidence without inventing facts.

### 6.2 Functional requirements

- Add a standalone `cover-letter` command and package workflow.
- Require company, role, canonical job identity, JD context, candidate source,
  and candidate-approved career narrative.
- Keep career narrative separate from ATS screening answers.
- Require structured LLM output with salutation, three to four paragraphs,
  closing, signature, and `evidence_claim_ids`.
- Validate every claim ID against the tagged resume source.
- Store claim IDs, prompt/template version, and JD/source hashes in an audit
  sidecar JSON; do not expose those annotations in the rendered PDF.
- Render with a dedicated simple ReportLab letter template.
- Validate with PyMuPDF that the result has exactly one page, non-empty text,
  required signature, and a defined word/character budget.
- Do not promote a "best effort" artifact if it spills to page two; generation
  fails instead.
- Cache using canonical job identity, JD hash, source hash, narrative hash,
  and prompt/template version.

### 6.3 Candidate configuration

```json
"career_narrative": {
  "reason_for_change": "Candidate-approved wording only",
  "next_role_priorities": ["AI product ownership", "customer impact"],
  "tone": "direct",
  "default_salutation": "Hiring Team",
  "do_not_claim": ["People-management experience"]
}
```

Missing narrative fields must be omitted, not inferred.

### 6.4 CLI and integration options

```powershell
python src/job_automation.py cover-letter `
  --url "https://..." `
  --company "Example" `
  --role "Product Manager" `
  --profile config/candidate_profile_config.json `
  --output output/Example_Product_Manager_Cover_Letter.pdf
```

- **Standalone generator, recommended:** lowest risk and easiest to review.
- **Orchestrator artifact mode:** add `--cover-letter off|generate|require`;
  generate only after JD context and personalised resume are available.
- **Generic ATS upload:** not part of the first release. Enable only when an
  individual ATS engine has a reviewed, testable cover-letter upload path.
- **Outreach attachment:** a generated cover letter may be attached alongside
  the resume in an outreach draft.

### 6.5 Acceptance criteria

- Missing JD context or required narrative fails clearly.
- Every recorded claim ID maps to valid tagged candidate evidence.
- A two-page PDF fails validation and is not promoted to the final output path.
- The output name is deterministic for equivalent job identity and inputs.
- No ATS upload or email send occurs during cover-letter generation.

## 7. Feature F3 — JD-contact extraction and outreach drafts

### 7.1 User story

As a candidate, I want to identify an explicitly published recruiter or hiring
contact for a role and prepare a fact-grounded Gmail draft with my tailored
resume attached.

### 7.2 Product flow

```text
Job URL or job context
    -> contact-discovery command
    -> output/contact_<job-key>.json
    -> outreach command
    -> reviewed Gmail draft
    -> optional explicit send
```

Contact discovery and message composition must remain separate from the normal
ATS application command. A future orchestration hook may run only after a
validated confirmed submission and only with an explicit outreach option.

### 7.3 Contact extraction requirements

Extract addresses from:

1. Official ATS JD HTML, including `mailto:` attributes.
2. Exact provider job metadata.
3. JSON-LD objects and nested job-page metadata.
4. User-supplied, approved official careers/contact pages.
5. A user-supplied contacts file.
6. A later approved enrichment-provider adapter.

Each contact must include source URL, retrieval time, bounded evidence excerpt,
signals, score, verification tier, and rejection reasons.

Hard-exclude malformed addresses and addresses with `no-reply`, `noreply`,
support, privacy, legal, security, or equivalent contexts.

### 7.4 Contact confidence policy

| Tier | Minimum evidence | Permitted action |
|---|---|---|
| `verified_official` | Literal email on an official job/careers source with job or recruiting context | Create draft; send only with explicit gates |
| `high_confidence` | Corroborated named recruiter/hiring lead with indirect or less job-specific evidence | Draft only |
| `unverified` | Weak context, provider-only claim, or inference | Record/report only |

Suggested score signals, without double-counting the same evidence:

- +45 literal `mailto:` or structured email.
- +30 literal email in visible official JD/career text.
- +20 official ATS or company-hosted source.
- +15 recruiter, talent, or hiring context near the address.
- +15 name and recruiting title paired with the address.
- +10 corroborated company-domain match.

Domain-pattern guesses are always `unverified`. A page that names a hiring
manager but supplies no paired address creates a named lead with an empty email;
the system must not derive an address from a pattern.

### 7.5 Outreach-draft requirements

- Add an `outreach` command and reusable Gmail composer.
- Refactor MIME construction into a testable function that accepts recipient,
  subject, body, optional HTML, attachments, and validated From address.
- Validate attachment files, MIME type, filename, and size before adding them.
- Attach the personalised resume PDF and optional cover-letter PDF with
  `EmailMessage.add_attachment(...)`.
- Create a Gmail draft by default for the outreach workflow.
- Direct send requires mutually exclusive `--send --yes` or the established
  interactive confirmation.
- Reject direct sends to `high_confidence` or `unverified` contacts.
- Only say "I applied" if the application registry/outcome proves a confirmed
  submission; otherwise use interest-oriented wording.
- Persist draft ID, message ID, thread ID, attachment hashes, selected contact
  evidence, template version, and job identity to deduplicate drafts.

### 7.6 CLI examples

```powershell
python src/job_automation.py contact-discovery `
  --url "https://boards.greenhouse.io/example/jobs/123" `
  --company "Example" `
  --role "Product Manager" `
  --source-mode official-only `
  --output output/contact_example_123.json

python src/job_automation.py outreach `
  --contact-result output/contact_example_123.json `
  --contact jane@example.com `
  --resume output/Example_Product_Manager_Resume.pdf `
  --cover-letter output/Example_Product_Manager_Cover_Letter.pdf `
  --draft
```

### 7.7 Acceptance criteria

- Default discovery never sends or creates Gmail artifacts.
- `mailto:` evidence and explicit official emails are retained with provenance.
- `no-reply` and similar addresses are rejected.
- Draft MIME contains the selected attachment(s) and no send call occurs.
- Direct send fails without explicit send gates and a verified-official contact.
- Repeated runs do not create duplicate drafts for the same job/contact/template
  unless an explicit force-new-draft option is used.

## 8. Feature F4 — SendAs alias audit and setup

### 8.1 User story

As a candidate, I want to know which configured email addresses can actually
be used as Gmail From identities and receive a safe setup path for the rest.

### 8.2 Audit requirements

Add a read-only `alias-audit` command that compares the configured
candidate email pool with Gmail's `users.settings.sendAs.list` response.

```powershell
python src/job_automation.py alias-audit `
  --pool config/candidate_email_pool.json `
  --json output/alias_audit.json `
  --csv output/alias_audit.csv
```

Classify only what the API proves:

- `ready`: Gmail primary address or `verificationStatus=accepted`.
- `pending_verification`: present with status `pending`.
- `missing_from_authenticated_account`: absent from the authenticated Gmail
  account.
- `invalid_config`: malformed or duplicate configured address.
- `unknown_api_denied`: API or authorization response does not prove status.

Do not call a missing address `not_permitted` unless an authorized setup or
admin-directory check supplies policy evidence.

The outreach composer must set a `From` header only when audit state says the
alias is ready. A plain address in the email pool is not permission to send
from it.

### 8.3 Setup paths and constraints

#### Personal Gmail or external owned address

- Provide a manual Gmail Settings checklist and re-audit flow.
- The address owner completes confirmation in Gmail.
- Do not promise that the desktop OAuth application can create or verify a
  SendAs alias programmatically.

#### Google Workspace user alias

- A Workspace administrator provisions the underlying user alias through the
  Admin Console or a separately authorized Directory API workflow.
- The user then configures the custom From address in Gmail.
- Workspace currently supports up to 30 aliases for a user; this limit and
  propagation behavior must be treated as admin-controlled infrastructure.

#### Domain-wide delegated service account

- This is a separate, explicitly opted-in Workspace-admin deployment.
- It may use `gmail.settings.sharing`, service-account impersonation, domain
  allowlists, exact-address confirmation, and an auditable `--apply` mode.
- It may call SendAs create/verify where Google permits it, but it does not
  provision the underlying Workspace directory alias itself.

#### Separate mailboxes

- Treat them as separate Gmail identities, not aliases.
- Use mailbox-specific OAuth or properly configured Workspace delegation.
- Do not position mailbox delegation as generic permission to send from any
  configured address.

### 8.4 Security requirements

- Never store SMTP credentials in the candidate profile, output, logs, or
  source control.
- Never automatically loop over unverified addresses to trigger setup changes.
- Gate any admin mutation with explicit target addresses, domain allowlists,
  a dry-run/report mode, and a human acknowledgment of ownership.

### 8.5 Acceptance criteria

- Audit works through current read-only Gmail access.
- The audit correctly distinguishes primary/accepted, pending, absent, invalid,
  and unknown states.
- No setup mutation happens in report mode.
- The selected sender identity remains consistent across resume, ATS submission,
  registry, and outreach artifacts.

## 9. Feature F5 — Recruiter and hiring-manager discovery

### 9.1 User story

As a candidate, I want useful, evidence-backed recruiter or hiring-manager
leads without pretending that a probabilistic match proves a person or email is
correct.

### 9.2 Scope and source order

Implement a separate `contact_discovery` service with source modes:

1. `official-only` — default and recommended.
2. `official-plus-user-supplied`.
3. `approved-provider` — only after the user selects a provider and accepts its
   commercial, privacy, and terms-of-use implications.
4. `include-inferred` — report-only; never automatically draftable or sendable.

Source preference:

1. Explicit email in the JD or official ATS page.
2. Official company careers/team page naming the recruiter or hiring manager.
3. User-supplied contact information.
4. Approved enrichment provider/API with retained provider record and retrieval
   date.
5. Domain-pattern inference only as a marked, unverified lead.

Do not scrape social-network sites or promote a guessed address to a sendable
contact.

### 9.3 Data model

```json
{
  "schema_version": 1,
  "job": {
    "canonical_url": "https://...",
    "ats": "greenhouse",
    "provider_job_id": "123",
    "company": "Example",
    "role": "Product Manager",
    "jd_sha256": "..."
  },
  "contacts": [
    {
      "email": "jane@example.com",
      "display_name": "Jane Recruiter",
      "contact_type": "recruiter",
      "association": "job_specific",
      "verification_level": "verified_official",
      "outreach_eligibility": "draft_allowed",
      "score": 90,
      "evidence": [
        {
          "source_type": "official_ats_jd",
          "source_url": "https://...",
          "retrieved_at": "2026-07-28T00:00:00Z",
          "location": "mailto_href",
          "excerpt": "For questions, contact Jane Recruiter...",
          "signals": ["literal_email", "recruiter_context", "official_source"]
        }
      ],
      "rejection_reasons": []
    }
  ],
  "errors": []
}
```

Persist the evidence necessary for review, but prefer source URL, hashes, and
bounded excerpts over unrestricted copies of pages or personal data.

### 9.4 Architecture and options

Suggested package modules:

- `contact_models.py`
- `contact_extraction.py`
- `contact_sources.py`
- `contact_discovery.py`
- `outreach.py`

Provider adapters should implement a narrow, injected interface that returns
provider, record ID, source URL, retrieval time, person/title/company data, and
any stated verification metadata. The discovery module must remain usable with
only official sources and manual input.

### 9.5 Acceptance criteria

- Named people without paired addresses remain leads, not inferred recipients.
- Every sendable/draftable address has retained evidence and tier.
- Inferred contacts cannot be sent to by CLI or orchestration hooks.
- Provider results show source, timestamp, and policy status.
- Discovery performs no Gmail activity and no implicit web crawling beyond
  selected, bounded sources.

## 10. Implementation sequence

1. Add `job_identity.py` and the applied-role registry.
2. Add `job_context.py` for all supported ATSs plus explicit JD-file fallback.
3. Build the standalone cover-letter generator and strict one-page validator.
4. Build contact extraction, evidence artifacts, and the Gmail attachment
   composer/draft workflow.
5. Add read-only SendAs audit and sender-identity resolution.
6. Add official/manual recruiter discovery, then approved provider adapters.
7. Build the optional Workspace-admin DWD alias utility only after confirming
   administrative authority and accepted operating policy.

## 11. Testing and release quality

All new units must follow the repository's current test conventions:

- No live ATS, browser, Gmail, LLM, or network calls in CI.
- Inject fakes for provider transport, Gmail service, LLM gateway, renderer,
  PDF reader, clock, and persistence boundaries.
- Use atomic artifact tests for every persisted schema.

Required test modules:

- `tests/test_application_registry.py`
- `tests/test_job_context.py`
- `tests/test_cover_letter.py`
- `tests/test_outreach.py`
- `tests/test_alias_audit.py`
- `tests/test_contact_discovery.py`

Minimum coverage scenarios:

- URL/provider identity aliases, historical import, registry skip-before-work,
  strict live-confirmation gating, and queue continuation.
- Missing JD/narrative, invalid claim IDs, exact-one-page enforcement, and no
  promoted cover letter after failed validation.
- Official-contact extraction, `mailto:` parsing, exclusion patterns,
  tier/action gating, and duplicate-draft prevention.
- MIME attachment correctness, validated From alias, and proof that draft mode
  does not call `messages.send`.
- Alias audit classifications and no mutation in report mode.
- Provider/manual/official discovery provenance and blocked inferred sends.

## 12. Decisions required before implementation

1. Registry backend: atomic JSON for sequential operation, or SQLite for
   concurrent workers and richer reporting.
2. Outreach policy: preview-first, draft-by-default, or a deliberately approved
   direct-send workflow for verified official contacts.
3. Sender model: one consistent primary address, or a ready-alias selector
   pinned per job/application.
4. Gmail account type: personal Gmail, Workspace user, Workspace admin with
   domain-wide delegation, or separate mailboxes.
5. Approved discovery sources/providers and their data-retention policy.
6. Whether cover letters should remain standalone/outreach-only or later become
   supported uploads for individual ATS engines.

## 13. External technical references

- [Gmail API: create and send messages, including attachments](https://developers.google.com/workspace/gmail/api/guides/sending)
- [Gmail API: SendAs resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.sendAs)
- [Gmail API: list SendAs aliases](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.sendAs/list)
- [Gmail API: create a SendAs alias](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.sendAs/create)
- [Gmail API: verify a SendAs alias](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.sendAs/verify)
- [Gmail Help: send from another address or alias](https://support.google.com/mail/answer/22370)
- [Google Workspace Admin Help: user email aliases](https://support.google.com/a/answer/33327)
- [Gmail API: create a mailbox delegate](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.settings.delegates/create)
