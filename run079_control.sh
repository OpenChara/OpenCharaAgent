#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
  exec uv run python -m scp079.control "$@"
else
  exec python3 -m scp079.control "$@"
fi
