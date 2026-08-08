# Contributing

Thank you for your interest in contributing to the Job Application Automation toolkit! This document provides guidelines for development and contribution. For system architecture, see [Architecture](docs/architecture.md). For documentation standards, see [Documentation Guide](docs/README.md).

## Local development

Use Python 3.10+, create a virtual environment, install both dependency files, and install Playwright Chromium when changing browser flows.

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
```

The preferred reproducible setup uses the committed uv lockfile:

```powershell
uv sync --locked --dev
uv run playwright install chromium
```

Run the full local quality suite before committing:

```powershell
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m compileall -q src
python -m pip check
```

## Change boundaries

Keep provider-specific browser behavior in `engines/`; keep workflow coordination in `core/`; keep resume and cover-letter concerns in `resume/`. Use `core.artifacts` for persisted files and preserve the `EngineResult` contract for application outcomes.

Never commit credentials, tokens, personal candidate data, generated application artifacts, or unredacted screenshots.

## Pull requests

Describe the user-visible behaviour, validation performed, and documentation changes. When changing a command, configuration schema, data artifact, or provider support boundary, update the relevant document under `docs/` and `CHANGELOG.md`.

---

**See Also:**
- [Architecture](docs/architecture.md) - System design and extension points
- [Documentation Guide](docs/README.md) - Documentation structure
- [CLI Reference](docs/cli-reference.md) - Command documentation
- [Data Formats](docs/data-formats.md) - Input/output specifications
