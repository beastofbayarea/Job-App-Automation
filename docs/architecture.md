# Architecture

The package is organized by workflow boundary, while `src/job_automation.py` is the single source-tree launcher and `job_application_automation.cli` performs lazy command dispatch.

```text
CLI
├── search: discovery, feeds, JSON-LD, liveness, cache, serialization
├── resume: source validation, AI/local generation, scoring, rendering
├── cover-letter: claims, AI generation, validation, rendering, cache
├── documents: paired generation, immutable manifests, pinned PuTTY transport
├── apply / queue: profile, runtime config, orchestration, submission log
├── engines: nine provider adapters plus guarded shared browser-form runtime
└── mail: Gmail OAuth/messages/persistence and email-pool selection
```

## Core boundaries

- `core/runtime_config.py` loads validated shared operational settings and resolves configured paths.
- `core/contracts.py` defines data passed between orchestrator and engines; `EngineResult` is the submission-confirmation boundary.
- `core/artifacts.py` atomically writes JSON, CSV, and text artifacts.
- `core/orchestrator.py` coordinates candidate profile, resume generation, provider detection, engine invocation, result persistence, and confirmed-submission logging.
- `core/identity.py` provides dependency-free canonical job URL, email, and lookup normalization.
- `core/document_archive.py` owns immutable manifests, opaque IDs, integrity checks, atomic local promotion, and the injected VPS transport boundary.
- `core/document_cli.py` composes paired generation with explicit archive storage and deterministic retrieval.
- `core/queue_runner.py` invokes live orchestration serially and checkpoints after every URL.

The detailed component diagram is maintained in [architecture.mmd](../architecture.mmd). The PRD is a living roadmap with per-feature implementation status; use the CLI reference and current code for delivered behavior.

## Extension points

Add an ATS by defining its host and job-path identity, implementing an engine with the shared result contract, registering it in CLI/orchestration provider selection, and adding selector plus safety-gate tests. Simple single-page providers should use the guarded shared browser-form runtime. Add new artifacts through `core.artifacts` rather than direct writes so they remain atomic and testable.
