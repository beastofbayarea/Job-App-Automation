# Architecture

This document describes the system architecture, component boundaries, and extension points. For implementation details, see the source code in `src/job_application_automation/`. For contributing guidelines, see [CONTRIBUTING](../CONTRIBUTING.md).


The package is organized by workflow boundary. `src/job_automation.py` is the
source-tree launcher, and `job_application_automation.cli` performs lazy command
dispatch without importing browser or network integrations until they are used.

```text
CLI
├── search facade
│   ├── discovery, JSON-LD, backlog, cache, and serialization
│   └── provider registry → Ashby / Greenhouse / Lever / SmartRecruiters / Workable
├── application facade
│   └── typed pipeline → safety → documents → engine → confirmation → checkpoint
├── continuous workers
│   └── shared runtime, sources, candidate selection, application, state, and pacing
├── engines
│   ├── provider navigation and provider-specific widgets
│   └── shared browser runtime, controls, and ordered section handlers
├── dashboard HTTP adapter
│   └── typed routes → artifact / operations / metrics / download services
└── resume, cover-letter, document archive, and mail services
```

## Core boundaries

- `core/runtime_config.py` loads validated operational settings and resolves
  configured paths. Configuration objects are the runtime API; consumers do not
  traverse unvalidated JSON dictionaries.
- `core/exceptions.py` defines the stable configuration, input, artifact,
  external-service, browser, blocked-application, and unknown-outcome failure
  families used at workflow boundaries.
- `core/contracts.py` defines data passed between orchestration and engines.
  `EngineResult` is the submission-confirmation boundary.
- `core/orchestrator.py` remains the public orchestration and compatibility
  facade. `core/application_pipeline.py` owns the typed stages, terminal
  checkpoints, result reconciliation, and cleanup-aware completion contract.
- `core/ats_urls.py` owns supported-provider URL recognition, while
  `core/identity.py` owns dependency-free canonical job URL, email, and lookup
  normalization.
- `core/artifacts.py` atomically writes JSON, CSV, and text artifacts and
  provides the interprocess lock used for shared read-modify-write files.
- `core/continuous_ats.py` and `core/continuous_source_ats.py` are provider and
  source entrypoints. The `core/continuous_worker_*` modules own their shared
  runtime, source loading, candidate selection, selected-job application,
  durable state, supervision, telemetry, pacing, and once-exit behavior.
- `core/document_archive.py` owns immutable manifests, opaque IDs, integrity
  checks, atomic local promotion, and the injected VPS transport boundary.
  `core/document_cli.py` composes paired generation with explicit archive
  storage and deterministic retrieval.
- `core/queue_runner.py` invokes live orchestration serially and checkpoints
  after every URL.

## Search, engine, and dashboard boundaries

- `search/job_boards.py` is the stable search facade. `search/providers/registry.py`
  owns provider recognition and dispatch, and each module under
  `search/providers/` owns one provider's feed and liveness behavior.
- `search/backlog.py` owns the single active, unsubmitted JSON list, strict
  ledger reconciliation, liveness-capable identity round trips, migration, and
  immediate confirmed-job pruning. It never owns an archive.
- `engines/browser_runtime.py` owns the browser session lifecycle;
  `engines/browser_controls.py` and `engines/form_sections.py` own reusable
  controls and ordered section execution. Provider modules retain navigation,
  provider-only widgets, submission gates, and compatibility entrypoints.
- `dashboard/server.py` is an unauthenticated, read-only HTTP adapter.
  `dashboard/routes.py` is the declarative dispatcher, while focused artifact,
  operations, metrics, and download modules implement the services. Every API
  route, including downloads, crosses this application boundary.

## Safety invariants

- Only an exact, durable `SUBMITTED & CONFIRMED` ledger entry is success.
  Blocked, ambiguous, and unknown outcomes remain quarantined for review.
- Browser screenshots are attempt-scoped and cleaned on every terminal path;
  durable JSON results, checkpoints, and submission evidence are retained.
- The public dashboard remains read-only and publishes only allow-listed,
  privacy-scrubbed fields and artifacts.

The CLI reference and current code define delivered behavior. The ATS support
guide distinguishes active adapters from historical prioritization and future
roadmap candidates.

## Extension points

Add an ATS by defining host and record-path identity in `core/ats_urls.py`,
implementing a search provider adapter and registering it in
`search/providers/registry.py`, then implementing an engine with the shared
result contract and registering its CLI entrypoint. Use the shared browser
runtime, controls, and section handlers where their contracts fit; keep
provider-only navigation and widgets in the provider module. Add registry,
production-path, selector, safety-gate, and confirmation tests. Add new
artifacts through `core.artifacts` so writes remain atomic and testable.

---

**See Also:**
- [CONTRIBUTING](../CONTRIBUTING.md) - Development setup and guidelines
- [Data Formats](data-formats.md) - Input/output file specifications
- [ATS Support](ats-support.md) - Provider-specific capabilities
- [FAQ](faq.md) - Frequently asked questions
