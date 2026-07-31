# Changelog

All notable user-facing changes are documented here. This project currently uses pre-1.0 versioning.

## [Unreleased]

### Added

- A single persistent `output/job_backlog.json` for active, unsubmitted jobs,
  with atomic merge, conservative liveness pruning, exact confirmed-submission
  removal, legacy search-output migration, and no archive/tombstone file.
- Four-file VPS publication and commit-coherent local pull support for the
  backlog alongside coverage, current CSV results, and the board cache,
  including a post-application publication when confirmations prune the list.
- First-class SmartRecruiters and Workable discovery, public-feed normalization,
  and provider-aware liveness checks in local and continuous VPS searches.
- Operational, configuration, CLI, data-format, security, architecture, ATS-support, and troubleshooting documentation.
- Contributor guidance for safe local development and test boundaries.
- VPS search-sync freshness reporting, dry-run generated-PDF pruning, an
  on-demand search trigger, and repository-aware logrotate installation.
- Private, immutable VPS storage and verified retrieval for paired CV and
  cover-letter PDFs, including a combined generation workflow.
- Atomic VPS run-stage status reporting with cron, repository, process, and
  artifact visibility in the bounded remote status helper.

### Changed

- Application workers now require matching permanent ledger evidence before
  marking a result confirmed and prune that confirmed URL from the active
  backlog without removing failed or manual-review attempts.
- Preserve authoritative provider closure when a later application-page check
  is uncertain, and retain unknown/backoff/server-error roles in the backlog.
- Normalize exact Ashby record URLs during legacy backlog migration so
  board-token case variants collapse without treating shared careers pages as
  record-specific.
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
