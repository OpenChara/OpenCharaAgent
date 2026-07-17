#!/usr/bin/env bash
# Thin convenience shim around the real CLI. The canonical launcher is
# `uv run chara`; this just forwards to it from a clone checkout.
#   ./run.sh            -> the roster / split TUI
#   ./run.sh --plain    -> the legacy plain terminal
#   ./run.sh <args...>  -> any `chara` subcommand (run, serve, desktop, ...)
set -eo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  exec uv run chara "$@"
else
  exec python3 -m chara.front.cli "$@"
fi
