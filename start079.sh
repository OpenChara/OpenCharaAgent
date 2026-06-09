#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
COOLDOWN="${1:-0.5}"
PROJECT_DIR="$(pwd)"
DISPLAY_CMD="cd '$PROJECT_DIR' && ./run079_display.sh --cooldown '$COOLDOWN'"
if command -v osascript >/dev/null 2>&1; then
  osascript -e "tell application \"Terminal\" to do script \"$DISPLAY_CMD\"" >/dev/null
  echo "Opened SCP-079 display terminal with cooldown=${COOLDOWN}s"
  echo "This terminal is now the operator control console."
  sleep 0.5
  ./run079_control.sh
else
  echo "osascript not found. Open another terminal and run:" >&2
  echo "  $DISPLAY_CMD" >&2
  echo "Then run ./run079_control.sh here." >&2
  exit 1
fi
