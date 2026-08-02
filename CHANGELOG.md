# Changelog

All notable user-facing changes are documented here. This project currently uses pre-1.0 versioning.

## [Unreleased]

### Added

- Opt-in, fail-open Sentry telemetry for unattended application, source,
  document-archive, and application-batch workers, with fixed event names,
  strict metadata allow-listing, and optional systemd environment loading.
- An explicit `structured` package extra for supported Pydantic 2 resume-schema
  decoding, installed and exercised by the locked CI environment.
- Reproducible uv dependency locking, automated uv and GitHub Actions updates,
  and CI auditing of the synchronized dependency environment.
- A single persistent `output/job_backlog.json` for active, unsubmitted jobs,
  with atomic merge, conservative liveness pruning, exact confirmed-submission
  removal, legacy search-output migration, and no archive/tombstone file.
- Four-file VPS publication and commit-coherent local pull support for the
  backlog alongside coverage, current CSV results, and the board cache,
  including a post-application publication when confirmations prune the list.
- First-class SmartRecruiters and Workable discovery, public-feed normalization,
  and provider-aware liveness checks in local and continuous VPS searches.
- Persistent SmartRecruiters and Workable VPS application workers with
  provider-specific installers and dynamically discovered status reporting.
- Operational, configuration, CLI, data-format, security, architecture, ATS-support, and troubleshooting documentation.
- Contributor guidance for safe local development and test boundaries.
- VPS search-sync freshness reporting, dry-run generated-PDF pruning, an
  on-demand search trigger, and repository-aware logrotate installation.
- Private, immutable VPS storage and verified retrieval for paired CV and
  cover-letter PDFs, including a combined generation workflow.
- Atomic VPS run-stage status reporting with cron, repository, process, and
  artifact visibility in the bounded remote status helper.

### Changed

- Unified direct and search/tracker continuous workers around typed state,
  source, recovery, supervision, telemetry, pacing, and once-exit contracts
  while preserving deployed CLI, service, and JSON state behavior.
- Extracted provider-neutral browser controls, Playwright session/runtime
  lifecycle, and typed ordered form-section handlers while retaining
  provider-specific navigation, widgets, and compatibility entry points.
- Decomposed application orchestration into typed safety, document-preparation,
  engine-execution, confirmation, and checkpoint stages while preserving the
  established CLI, result JSON, ledger, and screenshot-cleanup behavior.
- Split public job-board feed and liveness behavior into typed provider
  adapters whose registry now owns URL recognition, feed dispatch, and
  single/batch liveness dispatch while preserving the established search
  facade and CLI contracts.
- Centralized supported-ATS URL ownership and live candidate normalization so
  every application entrypoint rejects provider mismatches before generating
  documents or opening a browser.
- Built distributions now include every dashboard static asset and CI smoke
  tests the installed wheel rather than relying only on source-tree imports.
- Removed superseded private compatibility implementations and historical
  one-off design plans from the active repository documentation.
- Strict static typing now protects the core runtime-configuration, contract,
  submission-ledger, document-archive, and search-model boundaries in CI.
- Continuous ATS applications now run visibly inside their Xvfb session, and
  country-scoped work-authorization answers fail closed unless the candidate's
  profile explicitly lists the target country. Salary answers also retain their
  configured period instead of reusing annual compensation in monthly/current
  fields.
- Application workers now require matching permanent ledger evidence before
  marking a result confirmed and prune that confirmed URL from the active
  backlog without removing failed or manual-review attempts.
- Preserve authoritative provider closure when a later application-page check
  is uncertain, and retain unknown/backoff/server-error roles in the backlog.
- Normalize exact Ashby record URLs from stored and newly discovered jobs so
  board-token case variants collapse without treating shared careers pages as
  record-specific, while retaining authoritative provider metadata.
- Reconciled the PRD and historical implementation records with the delivered
  cover-letter and VPS-maintenance workflows, and clarified which documents are
  current operating references.
- Hardened VPS synchronization with commit-coherent output pulls, cron/manual
  mutual exclusion, shell-safe remote paths, temporary Plink password files,
  and offline regression coverage.
- Publish scheduled search snapshots before private work, bound daily document
  generation with fair retry capacity, and restrict automatic applications to
  jobs whose private document pair is already archived.
- Tightened confirmed-submission handling so test-mode results cannot be
  counted, prevented same-day submission-log collisions, and added a
  cover-letter email override for document identity consistency.
- Expanded the root README to cover every public command, compatibility alias,
  operational script, live-action boundary, output artifact, and exit-status
  convention.

### Fixed

- Empty application selections now replace stale orchestration results with an
  atomic empty snapshot, and live ledger/quarantine terminal jobs no longer
  consume candidate-email pool capacity or require a pool file.
- Live applications now fail closed on a corrupt submission ledger. A confirmed
  submission whose ledger write fails is durably quarantined for manual review,
  reported as non-success, and never retried automatically.
- Present but invalid runtime configuration now stops startup instead of being
  silently replaced with packaged defaults.
- Corrected dashboard installer remote-variable rendering, made the PowerShell
  timeout helper clean up child processes deterministically, and made runtime
  restart/status snapshots cover the enabled service topology.
- Corrected the Gmail API and authentication/configuration exit-code mapping in
  the CLI reference.
- Report missing, non-file, symlinked, unreadable, or invalidly encoded
  `cover-letter --jd-file` input as a clean CLI usage error instead of an
  uncaught traceback.
- Prevent unbounded document generation or one failed archive operation from
  indefinitely blocking VPS search publication and all guarded applications.

## [0.1.0]

### Added

- Public ATS job discovery for Greenhouse, Lever, Ashby, and generic JSON-LD pages.
- Tailored resume and cover-letter generation with validation and rendering.
- ATS-aware application orchestration, a confirmed-submission log, and sequential submission queue.
- Gmail OAuth utilities and configured candidate-email pool selection.
