from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from ..config import ROOT, LLMConfig


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


# ---- SEC-2: the provider api_key is GLOBAL, never copied per session ----------
# A living chara's session config holds only NON-secret overrides (provider/model/
# isolation/…); the secret is resolved at load from a GLOBAL store. This kills the
# old per-session duplication (N charas → N on-disk key copies, all needing rotation)
# and shrinks the leak surface — the key no longer sits inside each chara's own dir.
def _global_home() -> Path:
    # Same computation as sessions.lunamoth_home() (incl. .resolve()) so the key
    # the web keyring WRITES and the one this READS are the identical path.
    return Path(os.getenv("LUNAMOTH_HOME", Path.home() / ".lunamoth")).expanduser().resolve()


def global_api_key(provider: str = "", base_url: str = "") -> str:
    """Resolve the provider key from a GLOBAL store, never a per-session config.

    Order: the web keyring (~/.lunamoth/desktop.json) — a NAMED `keys` entry whose
    route (provider+base_url) matches this session's, else the default top-level
    key — then the CLI global config.json. The route match preserves the
    multi-key-per-chara feature (a chara woken with a named key still uses it)
    without storing the secret in the session. "" if none."""
    want_p = (provider or "").strip().lower()
    want_b = (base_url or "").strip().rstrip("/").lower()
    try:
        raw = json.loads((_global_home() / "desktop.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if isinstance(raw, dict):
        # A named key on the exact same route wins (e.g. a chara on an alt account).
        if want_b:
            keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
            for item in keys.values():
                if (isinstance(item, dict) and item.get("api_key")
                        and str(item.get("base_url") or "").strip().rstrip("/").lower() == want_b
                        and str(item.get("provider") or "").strip().lower() == want_p):
                    return str(item["api_key"])
        if str(raw.get("api_key") or ""):
            return str(raw["api_key"])
    try:
        raw2 = json.loads((_default_config_dir() / "config.json").read_text(encoding="utf-8"))
        if isinstance(raw2, dict) and str(raw2.get("api_key") or ""):
            return str(raw2["api_key"])
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def _is_session_config() -> bool:
    """True when CONFIG_PATH points at a per-session config (…/sessions/<name>/),
    not the global config — used to keep the secret out of session files."""
    try:
        sessions = (_global_home() / "sessions").resolve()
        return CONFIG_DIR == sessions or sessions in CONFIG_DIR.parents
    except Exception:  # noqa: BLE001
        return False


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
    py_backend: str = "sandbox"  # sandbox (OS jail, default) | admin (no jail, trusted operator)
    # SillyTavern-compatible persona. Empty character_path => built-in default persona.
    # The card is the ONE external file: its embedded character_book is the world.
    character_path: str = ""
    user_name: str = "操作者"
    # Composable tool pack (decoupled from the persona card). Name ('sandbox') or .json path.
    # Empty => no tools (pure roleplay). Any persona can be combined with any pack.
    toolpack: str = "sandbox"
    # The context window is NOT a setting for KNOWN models — it's the model's real
    # window, read from the provider (see providers.py). The ONE exception is a
    # custom / self-hosted endpoint whose window the provider can't report:
    # model_context (0 => auto: providers.py resolves it; >0 => explicit fallback,
    # required for custom models, ignored where the provider reports a real window).
    model_context: int = 0
    # Memory limits stay configurable (0 => auto: the card's value, else the
    # built-in default), since memory size can be characterful.
    memory_chars: int = 0
    user_chars: int = 0
    # TUI theme card (cosmetic skin: banner/colors/decoration). Empty => built-in theme.
    tui_theme_path: str = ""
    # Reasoning effort for thinking models: off | low | medium | high (default ON
    # at medium). Only sent to routes/models known to accept the parameter.
    reasoning: str = "medium"
    # Auxiliary vision model id (e.g. "google/gemini-3-flash", "openai/gpt-4o").
    # When the main model has no vision, an uploaded image is described by this
    # model and the text fed back. Empty => no auxiliary vision. Shares the main
    # base_url/api_key (any OpenRouter id reaches other providers).
    vision_model: str = ""
    # Show the thinking TEXT in the transcript (dimmed)? Default off: you get a
    # Claude-style "✶ thinking…" indicator instead, and the text leaves no trace.
    show_thinking: bool = False
    # Interaction mode — how the chara behaves while you are attached (see presence/):
    #   live = greets you, then keeps living its own loop while you watch (default)
    #   chat = greets you, then attends to you only — no self-talk while attached
    # (Detached life is not a mode: `lunamoth start/stop` is that switch.)
    mode: str = "live"
    # Engagement quiet period, seconds: while you are actively talking the chara
    # sets its own work aside; after this much silence it picks its life back up.
    quiet: int = 300
    # Max tool-call iterations in one turn. A chara doing real autonomous work
    # needs room (read → run → write → verify chains); the loop guardrails stop
    # genuinely-stuck repetition, so this can be generous. Operator-configurable.
    max_tool_steps: int = 80
    # Base seconds between spontaneous cycles. Card defaults may
    # supply this; operator commands/env/config persist an override.
    patience: float = 600.0
    # Internal source bit: distinguishes the default 600 from an operator
    # intentionally setting 600, so card defaults can still win when unset.
    patience_override: bool = False
    # Embodiment stance override. Empty means respect the card, then literal.
    # literal = tools are the chara's own hands; actor = tools are backstage.
    embodiment_override: str = ""

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
            reasoning=(self.reasoning or "medium").strip().lower(),
            vision_model=(self.vision_model or "").strip(),
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
    "user_name": ("LUNAMOTH_USER", "LUNAMOSS_USER"),
    "tui_theme_path": ("LUNAMOTH_THEME", "LUNAMOSS_THEME"),
    "toolpack": ("LUNAMOTH_TOOLPACK", "LUNAMOSS_TOOLPACK"),
    "memory_chars": ("LUNAMOTH_MEMORY_CHARS", "LUNAMOSS_MEMORY_CHARS"),
    "user_chars": ("LUNAMOTH_USER_CHARS",),
    "mode": ("LUNAMOTH_MODE", "LUNAMOTH_PRESENCE"),
    "reasoning": ("LLM_REASONING",),
    "vision_model": ("LLM_VISION_MODEL",),
    "patience": ("LUNAMOTH_PATIENCE",),
    "embodiment_override": ("LUNAMOTH_EMBODIMENT",),
}

_INT_FIELDS = {"max_tokens", "memory_chars", "user_chars", "quiet", "max_tool_steps"}

_FIELD_TYPES = {f.name: f.type for f in fields(Settings)}


def _coerce(name: str, raw: Any) -> Any:
    if name == "temperature":
        return float(raw)
    if name == "patience":
        return float(raw)
    if name in _INT_FIELDS:
        return int(raw)
    if name == "provider":
        return str(raw).strip().lower()
    if name == "mode":
        from ..presence import normalize_mode

        return normalize_mode(str(raw))
    if name == "reasoning":
        v = str(raw).strip().lower()
        return v if v in {"off", "low", "medium", "high"} else "medium"
    if name in {"show_thinking", "patience_override"}:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if name == "embodiment_override":
        v = str(raw).strip().lower()
        return v if v in {"literal", "actor"} else ""
    return str(raw)


_log = logging.getLogger("lunamoth.settings")


def _unique_target(base_dir: Path, stem: str) -> Path:
    target = base_dir / f"{stem}.json"
    n = 2
    while target.exists():
        target = base_dir / f"{stem}-{n}.json"
        n += 1
    return target


def _migrate_legacy_world(file_data: dict[str, Any]) -> None:
    """ONE-TIME migration for configs written before the world channel retired.

    A non-empty `world_path` pointing at an existing file is merged into the
    card this session uses (shared/bundled cards get a merged copy inside the
    config dir, and `character_path` is repointed); the config is then
    rewritten without `world_path`. A user's world is never dropped silently:
    if the merge cannot happen, the config is left untouched for a retry.
    """
    if "world_path" not in file_data:
        return
    world_path = str(file_data.get("world_path") or "").strip()
    rewrite = dict(file_data)
    rewrite.pop("world_path", None)
    if not world_path:
        # Nothing to merge — just drop the retired key.
        _rewrite_config(rewrite, file_data)
        return
    wp = Path(world_path)
    if not wp.is_file():
        _log.warning(
            "legacy world_path %s no longer exists — nothing to merge; "
            "dropping the retired world_path key from %s", world_path, CONFIG_PATH,
        )
        _rewrite_config(rewrite, file_data)
        return
    try:
        from ..content.cards import _card_json_from_png, merge_world_into_card
        from ..content.persona import default_character_path

        world = json.loads(wp.read_text(encoding="utf-8"))
        card_path = str(file_data.get("character_path") or "").strip()
        if not card_path:
            default_card = default_character_path()
            card_path = str(default_card) if default_card else ""
        if not card_path:
            raise FileNotFoundError("no character card to merge the world into")
        cp = Path(card_path)
        if cp.suffix.lower() == ".png":
            card = _card_json_from_png(cp)
        else:
            card = json.loads(cp.read_text(encoding="utf-8"))
        added = merge_world_into_card(card, world)
        # Cards inside the config dir are this session's own frozen copy —
        # merge in place. Anything else (bundled/shared/PNG) gets a merged
        # JSON copy in the config dir so other sessions never see the edit.
        in_place = cp.suffix.lower() == ".json" and CONFIG_DIR in cp.resolve().parents
        if in_place:
            target = cp
        else:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            target = _unique_target(CONFIG_DIR, cp.stem)
            rewrite["character_path"] = str(target)
            file_data["character_path"] = str(target)
        target.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        _rewrite_config(rewrite, file_data)
        _log.warning(
            "migrated legacy world book %s into the card %s (%d entr%s merged); "
            "config rewritten without world_path — the card is now the one file",
            world_path, target, added, "y" if added == 1 else "ies",
        )
    except Exception as e:  # keep the config untouched so nothing is lost
        _log.warning("legacy world_path migration failed (%s); config left as-is, will retry next launch", e)


def _rewrite_config(rewrite: dict[str, Any], file_data: dict[str, Any]) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(rewrite, ensure_ascii=False, indent=2), encoding="utf-8")
        file_data.pop("world_path", None)
    except OSError as e:
        _log.warning("could not rewrite %s during world_path migration: %s", CONFIG_PATH, e)


def load_settings() -> Settings:
    data: dict[str, Any] = {}
    # Seed from environment so existing env-based workflows still work pre-welcome-screen.
    for field_name, env_names in _ENV_MAP.items():
        for env_name in env_names:
            if os.environ.get(env_name):
                try:
                    data[field_name] = _coerce(field_name, os.environ[env_name])
                    if field_name == "patience":
                        data["patience_override"] = True
                    break
                except (TypeError, ValueError):
                    pass
    # The on-disk config (written by the welcome screen) is the source of truth and wins.
    if CONFIG_PATH.exists():
        try:
            file_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # Config written before the presence->mode rename.
            if "mode" not in file_data and file_data.get("presence"):
                file_data["mode"] = file_data["presence"]
            # Config written before the standalone world channel retired.
            _migrate_legacy_world(file_data)
            for k, v in file_data.items():
                if k in _FIELD_TYPES and v is not None:
                    try:
                        data[k] = _coerce(k, v)
                        if k == "patience" and "patience_override" not in file_data:
                            parsed = float(data[k])
                            if parsed > 0 and abs(parsed - 600.0) > 1e-9:
                                data["patience_override"] = True
                    except (TypeError, ValueError):
                        pass
        except (json.JSONDecodeError, OSError):
            pass
    # SEC-2: resolve the provider key from a GLOBAL store (env override > global
    # keyring), NOT from this (possibly per-session) config. The global wins over
    # any legacy copy embedded in a session file.
    env_key = os.environ.get("OPENAI_API_KEY") or ""
    global_key = global_api_key(str(data.get("provider") or ""), str(data.get("base_url") or ""))
    resolved = env_key or global_key or str(data.get("api_key") or "")
    data["api_key"] = resolved
    # Migration: a session config must not carry the secret. If a legacy one does
    # (and a global copy exists so we never orphan the only key), strip it on read.
    if global_key and _is_session_config() and CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("api_key"):
                raw.pop("api_key", None)
                CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass
    return Settings(**data)


def save_settings(settings: Settings) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(settings)
    # SEC-2: never persist the provider key into a per-session config — it lives in
    # the global keyring and is resolved at load. (The global config keeps it.)
    if _is_session_config():
        data.pop("api_key", None)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return CONFIG_PATH
