# Troubleshooting

## `python` cannot import the package

Run commands from the repository root after activating the virtual environment. For an installed command, run `python -m pip install .`. For source-tree use, run `python src/job_automation.py --help`.

## Browser automation cannot start

Install Chromium with `python -m playwright install chromium`. If using an existing browser through CDP, verify the configured endpoint in `runtime_config.json` is running and reachable. Retry in `--headed` mode to inspect the provider page.

## Gmail authorization fails

Confirm that `config/credentials.json` is an OAuth desktop-client credential and that the account has authorized the requested scope. Delete only the local token when reauthorization is intended; do not delete credentials casually. Check the Gmail command's exit status for OAuth/API failures.

## Vertex resume generation fails

Check the service-account file path, project ID, Vertex permissions, and model configuration. The resume workflow has a rule-based fallback when AI is unavailable, but review its output before use.

## Application did not submit or queue stopped

Inspect the application result, screenshot, and provider page. Queue execution stops whenever return status or confirmation evidence is insufficient. Verify the employer's confirmation before retrying, then resume with the correct zero-based `--start-index` only when duplicate submission is not possible.

## Search returns too few results

Read `output/job_search_coverage.json`. Broaden locations, provide known boards or career pages, use more than one ATS, and avoid an unnecessarily narrow date filter. `--require-live` intentionally excludes roles whose liveness cannot be confirmed.
