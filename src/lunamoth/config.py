from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SANDBOX_ROOT = Path(os.getenv("LUNAMOTH_SANDBOX", os.getenv("LUNAMOSS_SANDBOX", ROOT / "sandbox"))).resolve()


def content_dir(name: str) -> Path:
    """Resolve a bundled-content dir (``cards`` / ``toolpacks``).

    In a dev checkout these live at the repo root (``ROOT/<name>``). A WHEEL
    install has no repo root — ``ROOT`` points into site-packages — so the build
    (`scripts/build-wheel.sh`) copies them into ``lunamoth/_bundled/<name>``,
    shipped via package-data. Prefer the repo-root copy when present (dev / git
    install), else fall back to the packaged copy (wheel). Without this, a wheel
    deploy finds no toolpacks → every chara loses its tools, and no cards → no
    bundled personas (the 2026-06-17 deploy P0)."""
    root_copy = ROOT / name
    if root_copy.exists():
        return root_copy
    return Path(__file__).resolve().parent / "_bundled" / name


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
    # Anthropic prompt-cache TTL tier: "5m" (default) or "1h". 1h costs ~2x on
    # write vs 1.25x for 5m but amortizes across long sessions with >5-min gaps
    # between turns. Only applied on Anthropic-family routes (see core/cache.py).
    cache_ttl: str = os.getenv("LLM_CACHE_TTL", "5m").strip().lower()


@dataclass(frozen=True)
class ThoughtConfig:
    use_llm: bool = os.getenv("THOUGHT_USE_LLM", "true").strip().lower() in {"1", "true", "yes", "on"}
