#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'USAGE'
Open SCP 079 launcher

Default:
  ./run079.sh [--cooldown 0.5]
      Run SCP-079 in the current terminal.

Options:
  --cooldown <seconds>   Thought-loop pause, default 0.5
  --single              Same as default; kept for compatibility
  --display             Internal/legacy: run display terminal with FIFO input
  --control             Internal/legacy: run operator console for FIFO display
  --no-think            Forwarded to display runtime
  --no-clean-on-exit    Forwarded to display runtime
  --help                Show this help

Examples:
  ./run079.sh
  ./run079.sh --cooldown 0.5
  ./run079.sh --single --no-think
USAGE
}

MODE="single"
COOLDOWN="0.5"
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --single)
      MODE="single"
      shift
      ;;
    --display)
      MODE="display"
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

export LLM_PROVIDER="${LLM_PROVIDER:-openai_compatible}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://localhost:11434/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-ollama}"
export OPENAI_MODEL="${OPENAI_MODEL:-hf.co/bartowski/Llama-3.2-3B-Instruct-uncensored-GGUF:Q4_K_M}"

run_python() {
  if command -v uv >/dev/null 2>&1; then
    exec uv run python "$@"
  else
    exec python3 "$@"
  fi
}

case "$MODE" in
  single)
    run_python -m scp079.terminal --cooldown "$COOLDOWN" "${EXTRA[@]}"
    ;;
  display)
    mkdir -p sandbox/control
    run_python -m scp079.terminal --input-fifo sandbox/control/operator.in --cooldown "$COOLDOWN" "${EXTRA[@]}"
    ;;
  control)
    run_python -m scp079.control "${EXTRA[@]}"
    ;;
esac
