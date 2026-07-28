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

set -euo pipefail

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

# Push goes over SSH with the repo-scoped deploy key; clone/fetch stays on the
# origin remote's existing HTTPS URL since the repo is public and needs no
# credentials to read.
export GIT_SSH_COMMAND="ssh -i $DEPLOY_KEY -o IdentitiesOnly=yes"

cd "$REPO_DIR"
source .venv/bin/activate

python src/job_automation.py search \
  --role-type "Product Manager" \
  --ats-platform greenhouse \
  --ats-platform lever \
  --ats-platform ashby \
  --verify-live

if [ ! -d "$SYNC_DIR" ]; then
  git fetch origin "$BRANCH"
  git worktree add "$SYNC_DIR" "$BRANCH"
fi

for f in "${SYNC_FILES[@]}"; do
  mkdir -p "$SYNC_DIR/$(dirname "$f")"
  if [ -f "$REPO_DIR/$f" ]; then
    cp "$REPO_DIR/$f" "$SYNC_DIR/$f"
  fi
done

cd "$SYNC_DIR"
git add "${SYNC_FILES[@]}"
if ! git diff --cached --quiet; then
  git commit -m "vps: search run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git push "$PUSH_URL" "HEAD:refs/heads/$BRANCH"
else
  echo "No changes to sync."
fi
