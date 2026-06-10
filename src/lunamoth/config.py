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


@dataclass(frozen=True)
class ThoughtConfig:
    enabled_default: bool = os.getenv("ETERNAL_THINKING", "true").strip().lower() in {"1", "true", "yes", "on"}
    interval_seconds: float = float(os.getenv("THOUGHT_INTERVAL_SECONDS", "8"))
    max_visible_messages: int = int(os.getenv("MAX_VISIBLE_MESSAGES", "80"))
    max_session_thoughts: int = int(os.getenv("MAX_SESSION_THOUGHTS", "32"))
    use_llm: bool = os.getenv("THOUGHT_USE_LLM", "true").strip().lower() in {"1", "true", "yes", "on"}
