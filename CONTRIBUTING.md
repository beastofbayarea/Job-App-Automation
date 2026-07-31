# Contributing

## Local development

Use Python 3.10+, create a virtual environment, install both dependency files, and install Playwright Chromium when changing browser flows.

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest
```

The preferred reproducible setup uses the committed uv lockfile:

```powershell
uv sync --locked --dev
uv run playwright install chromium
uv run pytest
```

Run the full local quality suite before committing:

```powershell
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest
python -m compileall -q src
python -m pip check
```

## Change boundaries

Keep provider-specific browser behavior in `engines/`; keep workflow coordination in `core/`; keep resume and cover-letter concerns in `resume/`. Use `core.artifacts` for persisted files and preserve the `EngineResult` contract for application outcomes.

Tests run with sockets disabled. Mock browser, ATS, Gmail, and LLM boundaries; do not use live job boards, candidate accounts, or remote AI services in tests. Never commit credentials, tokens, personal candidate data, generated application artifacts, or unredacted screenshots.

## Pull requests

Describe the user-visible behaviour, safety impact, tests run, and documentation changes. When changing a command, configuration schema, data artifact, or provider support boundary, update the relevant document under `docs/` and `CHANGELOG.md`.
