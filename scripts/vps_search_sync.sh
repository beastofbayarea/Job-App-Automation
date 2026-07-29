#!/usr/bin/env bash
# Runs on the Hostinger VPS via cron. Executes the search workflow against the
# live repo checkout, then publishes only the search output files (no resumes,
# no PII) to a dedicated branch that the local machine pulls from.
#
# The vps-search-output branch already exists on origin (created once, ahead
# of time, from a scratch clone) with an initial output/job_search_coverage.json
# placeholder, so this script only ever fetches and fast-forwards it.
#
# One-time setup on the VPS before this script is scheduled:
#   1. git clone the repo, create a Python venv, `pip install -r requirements.txt`,
#      `playwright install --with-deps chromium`.
#   2. Generate a deploy key and add it to the GitHub repo (Settings > Deploy
#      keys) with "Allow write access" checked, scoped to this repo only:
#        ssh-keygen -t ed25519 -C "vps-search-sync" -f ~/.ssh/vps_search_sync -N ""
#   3. Add a cron entry, e.g.: 0 3 * * * REPO_DIR/scripts/vps_search_sync.sh >> REPO_DIR/output/vps_sync.log 2>&1
#   4. Install log rotation for the cron output so vps_sync.log doesn't grow
#      unbounded:
#        bash scripts/install_vps_logrotate.sh
#   5. Install Xvfb (`apt-get install -y xvfb`) so the application stage's
#      Chrome engines can launch on a display-less host. The engines always
#      launch a headed (non-headless) browser to avoid headless-detection by
#      ATS anti-bot checks, so a headless server needs a virtual display
#      instead of Playwright's own `headless=True` mode.

set -euo pipefail

# The application stage launches headed Chrome (never Playwright's headless
# mode, since ATS anti-bot checks can fingerprint headless browsers) even
# though this host has no physical display. Re-exec the whole run under a
# virtual X server so those browser launches succeed instead of crashing with
# "Missing X server or $DISPLAY". No-op when a real DISPLAY is already set or
# Xvfb isn't installed, so this stays safe on desktops and in test sandboxes.
if [ -z "${DISPLAY:-}" ] && command -v xvfb-run >/dev/null 2>&1; then
  exec xvfb-run -a --server-args="-screen 0 1280x1024x24" "$0" "$@"
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="vps-search-output"
SYNC_DIR="$REPO_DIR/.sync-worktree"
PUSH_URL="git@github.com:beastofbayarea/Job-App-Automation.git"
DEPLOY_KEY="$HOME/.ssh/vps_search_sync"
SYNC_FILES=(
  "output/job_search_coverage.json"
  "output/ai_jobs.csv"
  "output/ats_boards_cache.json"
)
PRIVATE_GENERATION_OUTPUT="$REPO_DIR/output/vps_generation_jobs.json"
DOCUMENT_STATE="$REPO_DIR/output/vps_document_archive_state.json"
APPLICATION_STATE="$REPO_DIR/output/vps_application_state.json"
APPLICATION_RESULTS="$REPO_DIR/output/vps_application_results"
APPLICATION_FAILURES="$REPO_DIR/output/vps_application_failures.json"
SUBMISSION_LOG="$REPO_DIR/output/submission_log.json"
RUN_STATUS="$REPO_DIR/output/vps_run_status.json"

# Cron and an on-demand trigger must never update the search artifacts or sync
# worktree concurrently. Keep the file descriptor open for the entire run.
cd "$REPO_DIR"
if ! command -v flock >/dev/null 2>&1; then
  echo "flock is required to serialize VPS search runs." >&2
  exit 69
fi
LOCK_FILE="$(git rev-parse --git-path vps-search-sync.lock)"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another VPS search sync is already running; no work was started." >&2
  exit 75
fi

# Push goes over SSH with the repo-scoped deploy key; clone/fetch stays on the
# origin remote's existing HTTPS URL since the repo is public and needs no
# credentials to read.
export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes"

source .venv/bin/activate

RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CURRENT_STAGE="initializing"
RUN_COMMIT="$(git rev-parse HEAD)"

write_run_status() {
  local state="$1"
  local exit_code="$2"
  local finished_at="$3"
  python - "$RUN_STATUS" "$RUN_STARTED_AT" "$CURRENT_STAGE" "$state" \
    "$exit_code" "$$" "$RUN_COMMIT" "$finished_at" <<'PY'
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

(
    status_path,
    started_at,
    stage,
    state,
    exit_code,
    pid,
    commit,
    finished_at,
) = sys.argv[1:]
path = Path(status_path)
path.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "version": 1,
    "state": state,
    "stage": stage,
    "started_at": started_at,
    "updated_at": datetime.now(UTC).isoformat(),
    "finished_at": finished_at or None,
    "exit_code": int(exit_code),
    "pid": int(pid),
    "commit": commit,
}
temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
temporary.write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
os.replace(temporary, path)
PY
}

set_stage() {
  CURRENT_STAGE="$1"
  write_run_status "running" 0 "" ||
    echo "Unable to update VPS run status for stage $CURRENT_STAGE." >&2
  printf 'VPS_RUN_STAGE stage=%s at=%s\n' \
    "$CURRENT_STAGE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

finish_run() {
  local exit_code=$?
  local state="success"
  local finished_at
  trap - EXIT
  if ((exit_code != 0)); then
    state="failed"
  fi
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  write_run_status "$state" "$exit_code" "$finished_at" ||
    echo "Unable to write the final VPS run status." >&2
  printf 'VPS_RUN_FINISH state=%s stage=%s exit_code=%s at=%s\n' \
    "$state" "$CURRENT_STAGE" "$exit_code" "$finished_at"
  exit "$exit_code"
}

trap finish_run EXIT
set_stage "search"

python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --ats-platform lever \
  --ats-platform ashby \
  --verify-live \
  --private-generation-output "$PRIVATE_GENERATION_OUTPUT"

set_stage "publication"
MISSING_SYNC_FILES=()
for f in "${SYNC_FILES[@]}"; do
  if [ ! -f "$REPO_DIR/$f" ]; then
    MISSING_SYNC_FILES+=("$f")
  fi
done
if ((${#MISSING_SYNC_FILES[@]} > 0)); then
  printf 'Search completed without required sync artifact: %s\n' \
    "${MISSING_SYNC_FILES[@]}" >&2
  exit 66
fi

if [ ! -d "$SYNC_DIR" ]; then
  git fetch origin "$BRANCH"
  git worktree add "$SYNC_DIR" "$BRANCH"
fi

for f in "${SYNC_FILES[@]}"; do
  mkdir -p "$SYNC_DIR/$(dirname "$f")"
  cp "$REPO_DIR/$f" "$SYNC_DIR/$f"
done

cd "$SYNC_DIR"
git add "${SYNC_FILES[@]}"
if ! git diff --cached --quiet; then
  git commit -m "vps: search run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git push "$PUSH_URL" "HEAD:refs/heads/$BRANCH"
else
  echo "No changes to sync."
fi

cd "$REPO_DIR"
set_stage "documents"
DOCUMENT_EXIT=0
PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
  python -m job_application_automation.core.search_documents \
  --input "$PRIVATE_GENERATION_OUTPUT" \
  --profile "$REPO_DIR/config/candidate_profile_config.json" \
  --vps-config "$REPO_DIR/config/vps_config.json" \
  --state "$DOCUMENT_STATE" \
  --launcher "$REPO_DIR/src/job_automation.py" || DOCUMENT_EXIT=$?

set_stage "applications"
APPLICATION_EXIT=0
PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
  python -m job_application_automation.core.search_applications \
  --input "$PRIVATE_GENERATION_OUTPUT" \
  --profile "$REPO_DIR/config/candidate_profile_config.json" \
  --launcher "$REPO_DIR/src/job_automation.py" \
  --results-dir "$APPLICATION_RESULTS" \
  --failure-report "$APPLICATION_FAILURES" \
  --submission-log "$SUBMISSION_LOG" \
  --document-state "$DOCUMENT_STATE" \
  --state "$APPLICATION_STATE" || APPLICATION_EXIT=$?

CURRENT_STAGE="finalizing"
if ((DOCUMENT_EXIT != 0)); then
  echo "One or more bounded document jobs failed; archived jobs were still eligible for the guarded application stage and failures will retry in later runs." >&2
fi
if ((APPLICATION_EXIT != 0)); then
  echo "One or more guarded application attempts failed; inspect the private failure report before any reviewed retry." >&2
  exit "$APPLICATION_EXIT"
fi
exit "$DOCUMENT_EXIT"
