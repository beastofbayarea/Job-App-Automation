# Changelog

All notable user-facing changes are documented here. This project currently uses pre-1.0 versioning.

## [Unreleased]

### Added

- Operational, configuration, CLI, data-format, security, architecture, ATS-support, and troubleshooting documentation.
- Contributor guidance for safe local development and test boundaries.
- VPS search-sync freshness reporting, dry-run generated-PDF pruning, an
  on-demand search trigger, and repository-aware logrotate installation.

### Changed

- Hardened VPS synchronization with commit-coherent output pulls, cron/manual
  mutual exclusion, shell-safe remote paths, temporary Plink password files,
  and offline regression coverage.

## [0.1.0]

### Added

- Public ATS job discovery for Greenhouse, Lever, Ashby, and generic JSON-LD pages.
- Tailored resume and cover-letter generation with validation and rendering.
- ATS-aware application orchestration, a confirmed-submission log, and sequential submission queue.
- Gmail OAuth utilities and configured candidate-email pool selection.
