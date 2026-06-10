from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .config import ROOT, LLMConfig


# Runtime config lives in the project, NOT inside the sandbox (the sandbox is
# zeroed on shutdown). It is gitignored so API keys never enter version control.
def _default_config_dir() -> Path:
    new, old = ROOT / ".lunamoth", ROOT / ".lunamoss"
    # Keep reading a pre-rename config dir until a new one is created.
    return old if old.is_dir() and not new.is_dir() else new


CONFIG_DIR = Path(os.getenv("LUNAMOTH_CONFIG_DIR", os.getenv("LUNAMOSS_CONFIG_DIR", _default_config_dir()))).resolve()
CONFIG_PATH = CONFIG_DIR / "config.json"


def config_path() -> Path:
    return CONFIG_PATH


# Real LLM providers that go through the OpenAI-compatible HTTP path.
LIVE_PROVIDERS = {"openai_compatible", "openai", "ollama", "openrouter"}


@dataclass
class Settings:
    """Mutable runtime settings, edited by the welcome screen and persisted to disk."""

    provider: str = "mock"
    base_url: str = ""
    api_key: str = ""
    # Matches the OpenRouter preset (the recommended first-run path); the wizard
    # overwrites this with whatever preset/model the operator actually picks.
    model: str = "deepseek/deepseek-v4-flash"
    temperature: float = 0.85
    # Reply/tool-call token budget. Must be generous: tool-call arguments (e.g. a
    # whole file written via the `terminal` tool) stream inside this budget, and a
    # tiny cap truncates the arguments JSON mid-write → "missing argument" errors.
    max_tokens: int = 4096
    # NOTE: there is deliberately no `lang` setting. Language is not a user choice —
    # it is a property of the active character card (a .zh card speaks zh, a .en card
    # speaks en). The engine and tools are language-agnostic.
    py_backend: str = "sandbox"  # local (dir-level) | sandbox (OS jail, default) | docker
    # SillyTavern-compatible persona. Empty character_path => built-in default persona.
    character_path: str = ""
    world_path: str = ""
    user_name: str = "操作者"
    # Composable tool pack (decoupled from the persona card). Name ('sandbox') or .json path.
    # Empty => no tools (pure roleplay). Any persona can be combined with any pack.
    toolpack: str = "sandbox"
    # Resource limits as an independent layer (Overdrive). 0 => auto: use the card's
    # extensions value if present, else the built-in default. Non-zero => explicit override.
    context_tokens: int = 0
    memory_chars: int = 0
    memory_tokens: int = 0
    # TUI theme card (cosmetic skin: banner/colors/decoration). Empty => built-in LunaMoth theme.
    tui_theme_path: str = ""
    # Presence awareness mode (Claude-Code-style global mode, see presence.py):
    #   auto   = greet on attach, hold the forever loop until the operator speaks
    #   always = greet on attach, never wait
    #   off    = no presence events; the character never self-starts
    presence: str = "auto"

    def is_live(self) -> bool:
        return self.provider.strip().lower() in LIVE_PROVIDERS and bool(self.base_url.strip())

    def to_llm_config(self) -> LLMConfig:
        # "openrouter" is just an OpenAI-compatible endpoint as far as the client is concerned.
        provider = "openai_compatible" if self.provider == "openrouter" else self.provider
        return LLMConfig(
            provider=provider.strip().lower(),
            base_url=self.base_url.rstrip("/"),
            api_key=self.api_key,
            model=self.model,
            temperature=float(self.temperature),
            max_tokens=int(self.max_tokens),
        )


# Provider presets: selecting one fills base_url (and a sensible default model);
# the operator still supplies api_key / model where needed.
PRESETS: dict[str, dict[str, Any]] = {
    "OpenRouter": {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-v4-flash",
    },
    "OpenAI": {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "Ollama (local)": {
        "provider": "openai_compatible",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "model": "qwen2.5:3b-instruct",
    },
    "Mock (offline)": {
        "provider": "mock",
        "base_url": "",
        "api_key": "",
        "model": "mock",
    },
}


# Map each Settings field to the env var that can seed it (precedence: defaults < env < file).
_ENV_MAP: dict[str, tuple[str, ...]] = {
    "provider": ("LLM_PROVIDER",),
    "base_url": ("OPENAI_BASE_URL",),
    "api_key": ("OPENAI_API_KEY",),
    "model": ("OPENAI_MODEL",),
    "temperature": ("LLM_TEMPERATURE",),
    "max_tokens": ("LLM_MAX_TOKENS",),
    "py_backend": ("LUNAMOTH_PY_BACKEND", "LUNAMOSS_PY_BACKEND"),
    "character_path": ("LUNAMOTH_CHARACTER", "LUNAMOSS_CHARACTER"),
    "world_path": ("LUNAMOTH_WORLD", "LUNAMOSS_WORLD"),
    "user_name": ("LUNAMOTH_USER", "LUNAMOSS_USER"),
    "tui_theme_path": ("LUNAMOTH_THEME", "LUNAMOSS_THEME"),
    "toolpack": ("LUNAMOTH_TOOLPACK", "LUNAMOSS_TOOLPACK"),
    "context_tokens": ("LUNAMOTH_CONTEXT_TOKENS", "LUNAMOSS_CONTEXT_TOKENS"),
    "memory_chars": ("LUNAMOTH_MEMORY_CHARS", "LUNAMOSS_MEMORY_CHARS"),
    "memory_tokens": ("LUNAMOTH_MEMORY_TOKENS", "LUNAMOSS_MEMORY_TOKENS"),
    "presence": ("LUNAMOTH_PRESENCE",),
}

_INT_FIELDS = {"max_tokens", "context_tokens", "memory_chars", "memory_tokens"}

_FIELD_TYPES = {f.name: f.type for f in fields(Settings)}


def _coerce(name: str, raw: Any) -> Any:
    if name == "temperature":
        return float(raw)
    if name in _INT_FIELDS:
        return int(raw)
    if name == "provider":
        return str(raw).strip().lower()
    if name == "presence":
        from .presence import normalize_mode

        return normalize_mode(str(raw))
    return str(raw)


def load_settings() -> Settings:
    data: dict[str, Any] = {}
    # Seed from environment so existing env-based workflows still work pre-welcome-screen.
    for field_name, env_names in _ENV_MAP.items():
        for env_name in env_names:
            if os.environ.get(env_name):
                try:
                    data[field_name] = _coerce(field_name, os.environ[env_name])
                    break
                except (TypeError, ValueError):
                    pass
    # The on-disk config (written by the welcome screen) is the source of truth and wins.
    if CONFIG_PATH.exists():
        try:
            file_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in file_data.items():
                if k in _FIELD_TYPES and v is not None:
                    try:
                        data[k] = _coerce(k, v)
                    except (TypeError, ValueError):
                        pass
        except (json.JSONDecodeError, OSError):
            pass
    return Settings(**data)


def save_settings(settings: Settings) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return CONFIG_PATH
