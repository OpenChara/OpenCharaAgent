#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
FIFO="sandbox/control/operator.in"
if [[ ! -p "$FIFO" ]]; then
  echo "FIFO not found. Start ./run079_display.sh first." >&2
  exit 1
fi
if [[ $# -gt 0 ]]; then
  printf '%s\n' "$*" > "$FIFO"
else
  while IFS= read -r line; do
    printf '%s\n' "$line" > "$FIFO"
  done
fi
