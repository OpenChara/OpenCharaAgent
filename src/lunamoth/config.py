from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SANDBOX_ROOT = Path(os.getenv("LUNAMOTH_SANDBOX", os.getenv("LUNAMOSS_SANDBOX", ROOT / "sandbox"))).resolve()


@dataclass(frozen=True)
class LLMConfig:
    provider: str = os.getenv("LLM_PROVIDER", "mock").strip().lower()
    base_url: str = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
    api_key: str = os.getenv("OPENAI_API_KEY", "")
    model: str = os.getenv("OPENAI_MODEL", "deepseek/deepseek-v4-flash")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.85"))
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))
    # Reasoning effort for thinking models: off | low | medium | high.
    # Default ON at medium; only sent to routes/models known to accept it.
    reasoning: str = os.getenv("LLM_REASONING", "medium").strip().lower()
    # Vision is a model CAPABILITY, not a preference — auto-detected by name.
    # `on`/`off` is a safety valve for routes the name heuristic can't read
    # (a custom-named vision model, or a text-only one that fakes a vision name).
    vision: str = os.getenv("LLM_VISION", "auto").strip().lower()


@dataclass(frozen=True)
class ThoughtConfig:
    use_llm: bool = os.getenv("THOUGHT_USE_LLM", "true").strip().lower() in {"1", "true", "yes", "on"}
