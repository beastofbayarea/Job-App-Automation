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

Inspect the application result, diagnostic logs, submission ledger, and provider page. Screenshots are removed automatically after the attempt terminates. Queue execution stops whenever return status or confirmation evidence is insufficient. Verify the employer's confirmation before retrying, then resume with the correct zero-based `--start-index` only when duplicate submission is not possible.

## Search returns too few results

Read `output/job_search_coverage.json`. Broaden locations, provide known boards or career pages, use more than one ATS, and avoid an unnecessarily narrow date filter. `--require-live` intentionally excludes roles whose liveness cannot be confirmed.

## Job backlog is missing, stale, or unexpectedly retains a role

The VPS search command must include
`--backlog-output output/job_backlog.json`. Check the coverage report's
`backlog` object and the remote status helper's backlog timestamp. A timeout,
`429`, `5xx`, bot block, malformed response, or uncertain page identity is not
proof that a job closed, so the role is intentionally retained. Only an exact
`SUBMITTED & CONFIRMED` ledger match or conclusive provider/page closure can
remove it.

The local pull requires coverage, current CSV, board cache, and backlog from
one remote commit. If the remote generated-data branch predates backlog
support, deploy current `main` and let one successful VPS search publish the
first four-file snapshot; the pull safely leaves all local files unchanged
until then.

## VPS cron is installed but search output is stale

Run `pwsh scripts\check_vps_automation_status.ps1`. Compare the cron entry,
process list, repository commit, `vps_run_status.json`, artifact timestamps,
and log tail. Search publication now precedes bounded document generation, so
an old public snapshot with a run stuck in `documents` usually means the VPS is
running older code. Deploy the current `main` commit before retrying. Do not
start a second run while the lock holder is active.

If the status helper times out before printing remote state, verify the VPS
provider status and try the provider console or a network path that can receive
the SSH banner. The helper exits after its configured timeout instead of
leaving a hidden `plink` process running.

## VPS document archive cannot connect

Confirm that PuTTY `plink` and `pscp` are on `PATH`, the ignored VPS config has the dedicated archive user, and `ssh_host_key` is the trusted PuTTY-format fingerprint. An unknown or changed host key fails closed in batch mode; verify changes independently instead of bypassing the pin. If using `archive_private_key_file`, confirm it points to an existing dedicated `.ppk`.

## Archive already exists with different content

Records are immutable for one canonical job URL and normalized email. A different company/title or PDF under that identity is reported as a conflict and is not overwritten. Verify the URL/email and whether the existing record is the reviewed document set before changing anything on the VPS.

## Retrieved document fails verification

No downloaded file is promoted when the manifest, identity, size, PDF signature, or SHA-256 check fails. Preserve the local archive output, inspect VPS/backups for corruption, and do not use the partial temporary download.
