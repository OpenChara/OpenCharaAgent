#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'USAGE'
LunaMoss launcher

Default:
  ./run.sh [--cooldown 0.5]
      Run the single-terminal split TUI.

Options:
  --cooldown <seconds>   Self-talk loop pause, default 2.0
  --plain               Legacy plain terminal mode
  --forever             Start with the eternal self-talk loop ON (default: OFF)
  --no-clean-on-exit    Do not clean runtime sandbox on shutdown
  --help                Show this help

Examples:
  ./run.sh
  ./run.sh --forever --cooldown 4
  ./run.sh --plain
USAGE
}

MODE="tui"
COOLDOWN="2.0"
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --plain|--single)
      MODE="plain"
      shift
      ;;
    --display)
      MODE="plain"
      EXTRA+=("--input-fifo" "sandbox/control/operator.in")
      shift
      ;;
    --control)
      MODE="control"
      shift
      ;;
    --cooldown)
      COOLDOWN="${2:-0.5}"
      shift 2
      ;;
    --cooldown=*)
      COOLDOWN="${1#--cooldown=}"
      shift
      ;;
    --*)
      EXTRA+=("$1")
      shift
      ;;
    *)
      COOLDOWN="$1"
      shift
      ;;
  esac
done

# The LLM backend is configured in the welcome screen (persisted to .lunamoss/config.json).
# Any LLM_PROVIDER / OPENAI_* env vars set here still work as a fallback seed when no
# config file exists yet, but we intentionally do NOT hardcode a provider anymore.

run_python() {
  if command -v uv >/dev/null 2>&1; then
    exec uv run python "$@"
  else
    exec python3 "$@"
  fi
}

case "$MODE" in
  tui)
    run_python -m lunamoss.tui --cooldown "$COOLDOWN" "${EXTRA[@]}"
    ;;
  plain)
    run_python -m lunamoss.terminal --cooldown "$COOLDOWN" "${EXTRA[@]}"
    ;;
  control)
    run_python -m lunamoss.control "${EXTRA[@]}"
    ;;
esac
