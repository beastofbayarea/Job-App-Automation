#!/usr/bin/env bash
# Renders the repository-specific log path into the logrotate template and
# installs it. Use --stdout to inspect the rendered configuration without
# changing the host.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_DIR/scripts/templates/vps-sync.logrotate"
LOG_PATH="$REPO_DIR/output/vps_sync.log"
DESTINATION="/etc/logrotate.d/vps-sync"
PRINT_ONLY=false

while (($#)); do
  case "$1" in
    --destination)
      if (($# < 2)); then
        echo "--destination requires a path." >&2
        exit 64
      fi
      DESTINATION="$2"
      shift 2
      ;;
    --stdout)
      PRINT_ONLY=true
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

if [[ "$LOG_PATH" == *'"'* || "$LOG_PATH" == *'\'* || "$LOG_PATH" == *$'\n'* ]]; then
  echo "The repository path contains characters unsupported by this logrotate installer." >&2
  exit 64
fi

render_config() {
  local replacement_count=0
  local line

  while IFS= read -r line || [[ -n "$line" ]]; do
    # Git may check the template out with CRLF on Windows. Normalize the line
    # before exact placeholder matching so --stdout remains cross-platform.
    line="${line%$'\r'}"
    if [[ "$line" == "@VPS_SYNC_LOG_PATH@ {" ]]; then
      printf '"%s" {\n' "$LOG_PATH"
      replacement_count=$((replacement_count + 1))
    else
      printf '%s\n' "$line"
    fi
  done <"$TEMPLATE"

  if ((replacement_count != 1)); then
    echo "Expected exactly one log-path placeholder in $TEMPLATE." >&2
    return 1
  fi
}

if [[ "$PRINT_ONLY" == true ]]; then
  render_config
  exit 0
fi

TEMP_CONFIG="$(mktemp)"
trap 'rm -f "$TEMP_CONFIG"' EXIT
render_config >"$TEMP_CONFIG"

if ((EUID == 0)); then
  install -m 0644 "$TEMP_CONFIG" "$DESTINATION"
else
  sudo install -m 0644 "$TEMP_CONFIG" "$DESTINATION"
fi

echo "Installed logrotate policy for $LOG_PATH at $DESTINATION"
