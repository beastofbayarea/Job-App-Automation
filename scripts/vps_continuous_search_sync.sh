#!/usr/bin/env bash
# Continuous VPS search, document generation, and application runner.
# Executes search & sync in an endless loop under systemd supervision.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CYCLE_PAUSE_SECONDS="${CYCLE_PAUSE_SECONDS:-300}"

cd "$REPO_DIR"

printf 'Starting continuous VPS search sync worker at %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

while true; do
  printf '=== Beginning VPS search sync cycle at %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if bash "$REPO_DIR/scripts/vps_search_sync.sh"; then
    printf 'VPS search sync cycle completed successfully at %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  else
    printf 'VPS search sync cycle finished with exit status %s at %s\n' "$?" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi

  printf 'Sleeping for %s seconds before next cycle...\n' "$CYCLE_PAUSE_SECONDS"
  sleep "$CYCLE_PAUSE_SECONDS"
done
