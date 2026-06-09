#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export LLM_PROVIDER="${LLM_PROVIDER:-openai_compatible}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://localhost:11434/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-ollama}"
export OPENAI_MODEL="${OPENAI_MODEL:-hf.co/bartowski/Llama-3.2-3B-Instruct-uncensored-GGUF:Q4_K_M}"
if command -v uv >/dev/null 2>&1; then
  exec uv run python -m scp079.terminal "$@"
else
  exec python3 -m scp079.terminal "$@"
fi
