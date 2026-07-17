from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from ..config import ROOT, LLMConfig
from ..content.knobs import DEFAULT_PATIENCE, DEFAULT_QUIET, patience_is_explicit


# Runtime config lives in the project, NOT inside the sandbox (the sandbox is
# zeroed on shutdown). It is gitignored so API keys never enter version control.
def _default_config_dir() -> Path:
    new, old = ROOT / ".chara", ROOT / ".lunamoss"
    # Keep reading a pre-rename config dir until a new one is created.
    return old if old.is_dir() and not new.is_dir() else new


CONFIG_DIR = Path(os.getenv("CHARA_CONFIG_DIR", os.getenv("LUNAMOSS_CONFIG_DIR", _default_config_dir()))).resolve()
CONFIG_PATH = CONFIG_DIR / "config.json"


def config_path() -> Path:
    return CONFIG_PATH


# ---- SEC-2: the provider api_key is GLOBAL, never copied per session ----------
# A living chara's session config holds only NON-secret overrides (provider/model/
# isolation/…); the secret is resolved at load from a GLOBAL store. This kills the
# old per-session duplication (N charas → N on-disk key copies, all needing rotation)
# and shrinks the leak surface — the key no longer sits inside each chara's own dir.
def _global_home() -> Path:
    # Same computation as sessions.chara_home() (incl. .resolve()) so the key
    # the web keyring WRITES and the one this READS are the identical path.
    return Path(os.getenv("CHARA_HOME", Path.home() / ".chara")).expanduser().resolve()


# The webui provider presets (id · label · base_url) — the ONE backend copy. The SPA's
# apps/web/src/lib/providers.ts mirrors it (CSS/TS can't import Python; pinned equal by a
# cross-language drift test). A key folded out of the legacy top-level slot is labelled by
# these too, so it lands under the right Providers-pane row.
PROVIDER_PRESETS: tuple[dict[str, str], ...] = (
    {"provider": "openrouter", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
    {"provider": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    {"provider": "volcano", "label": "火山引擎", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    {"provider": "hunyuan", "label": "混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1"},
    {"provider": "dashscope", "label": "阿里云", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
)
_PROVIDER_LABELS = {p["provider"]: p["label"] for p in PROVIDER_PRESETS}


def normalize_base_url(s: object) -> str:
    """Canonical base_url for ROUTE COMPARISON: trimmed, no trailing slash, lower-cased
    (the host is case-insensitive). The ONE normalizer the keyring route match (here) and
    the hub's route comparison (server/hub/config) share, so a key always matches its own
    route regardless of cosmetic case/slash differences."""
    return str(s or "").strip().rstrip("/").lower()


_norm = normalize_base_url  # short internal alias used by the keyring matchers


def migrate_legacy_default_key() -> None:
    """One-time: fold a legacy top-level desktop.json ``api_key`` into the keyring (the
    ONE key store) and drop the top-level secret. The keyring is now the single source
    of provider keys; the active selection is the NON-secret ``active_key_label`` (+
    provider/base_url/model). Idempotent — a no-op once there is no top-level key."""
    path = _global_home() / "desktop.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    key = str(raw.get("api_key") or "")
    if not key:
        return  # already migrated / never had a top-level key
    provider = str(raw.get("provider") or "")
    base_url = str(raw.get("base_url") or "")
    keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
    # Reuse an existing keyring entry on the same route (the common webui case where
    # use_key already wrote one); otherwise synthesize one from the top-level route.
    match = next((lbl for lbl, it in keys.items()
                  if isinstance(it, dict) and it.get("api_key")
                  and _norm(it.get("base_url")) == _norm(base_url)
                  and _norm(it.get("provider")) == _norm(provider)), None)
    if match is None:
        label = _PROVIDER_LABELS.get(_norm(provider).rstrip("/"), "") or provider or "default"
        if label in keys:  # don't clobber a differently-routed entry under this label
            label = f"{label} · {base_url or provider}"
        keys[label] = {"provider": provider, "base_url": base_url,
                       "api_key": key, "model": str(raw.get("model") or "")}
        match = label
    raw["keys"] = keys
    if not str(raw.get("active_key_label") or ""):
        raw["active_key_label"] = match  # the legacy top-level key WAS the active default
    raw.pop("api_key", None)  # the keyring is the source now — drop the duplicate secret
    from ..config import atomic_write_text
    try:
        # atomic (temp + os.replace, 0600): a crash mid-migration must never tear the
        # keyring file (it holds every provider secret).
        atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2), private=True)
    except OSError:
        pass


def global_api_key(provider: str = "", base_url: str = "") -> str:
    """Resolve the provider key from the GLOBAL keyring — the ONE key store.

    The keyring is ~/.chara/desktop.json ``keys`` (written by the webui AND the
    terminal setup flows, via save_global_key). An EXPLICIT route (provider+base_url)
    → the named entry on that exact route (the multi-key-per-chara / alt-account
    feature). An EMPTY base_url means "the active default route" → the entry named by
    the top-level ``active_key_label`` (else a provider match). "" if none.

    There is no config.json fallback any more: config.json never stores a secret, so
    a single store can't drift from a stale CLI copy (env OPENAI_API_KEY still wins,
    applied by load_settings)."""
    want_p = _norm(provider)
    want_b = _norm(base_url)
    try:
        raw = json.loads((_global_home() / "desktop.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        return ""
    if raw.get("api_key"):  # legacy top-level key present → fold it into the keyring once
        migrate_legacy_default_key()
        try:
            raw = json.loads((_global_home() / "desktop.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
    if want_b:
        # Explicit route → the named entry on that exact provider+base_url.
        for item in keys.values():
            if (isinstance(item, dict) and item.get("api_key")
                    and _norm(item.get("base_url")) == want_b
                    and _norm(item.get("provider")) == want_p):
                return str(item["api_key"])
    else:
        # Default route → the active label's key (then a provider match as a backstop).
        active = keys.get(str(raw.get("active_key_label") or ""))
        if (isinstance(active, dict) and active.get("api_key")
                and (not want_p or _norm(active.get("provider")) == want_p)):
            return str(active["api_key"])
        for item in keys.values():
            if (isinstance(item, dict) and item.get("api_key")
                    and _norm(item.get("provider")) == want_p):
                return str(item["api_key"])
    return ""


def save_global_key(provider: str, base_url: str, api_key: str, *,
                    model: str = "", label: str = "") -> str:
    """Write a provider key into the GLOBAL keyring (~/.chara/desktop.json ``keys``)
    — the ONE key store, shared with the webui — and make it the active default route.

    The terminal setup flows (wizard / TUI welcome) call this so a CLI-entered key
    lands in the same store the desktop app reads, NOT a second config.json copy.
    Reuses an existing entry on the same provider+base_url route (no duplicate label
    on re-run). Returns the label written; "" (no-op) on an empty key."""
    api_key = (api_key or "").strip()
    if not api_key:
        return ""
    provider = (provider or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    path = _global_home() / "desktop.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
    if not label:
        # Reuse an existing entry on the same route; else label by the provider preset.
        label = next((lbl for lbl, it in keys.items()
                      if isinstance(it, dict)
                      and _norm(it.get("base_url")) == _norm(base_url)
                      and _norm(it.get("provider")) == _norm(provider)), "")
        if not label:
            label = _PROVIDER_LABELS.get(_norm(provider), "") or provider or "default"
    prev = keys.get(label) if isinstance(keys.get(label), dict) else {}
    keys[label] = {"provider": provider, "base_url": base_url, "api_key": api_key,
                   "model": model or str(prev.get("model") or "")}
    raw["keys"] = keys
    raw["active_key_label"] = label  # this is now the active default route
    raw["provider"] = provider       # keep the keyring's default-route fields in sync
    raw["base_url"] = base_url
    if model:
        raw["model"] = model
    raw.pop("api_key", None)  # never a top-level secret — the keys map is the store
    from ..config import atomic_write_text
    atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2), private=True)
    return label


def global_vision_route() -> dict[str, str]:
    """The DEFAULT vision route: the read-image model (Settings · 模型 · 读图 →
    ``vision_model``) on its OWN provider (``vision_provider`` = a saved keyring
    label), independent of BOTH the chara's provider and the main text default —
    so a chara on OpenRouter can read images via, say, an Alibaba (DashScope)
    vision model. Vision needs no prompt cache, so it never rides the chara.
    Falls back to the main default provider when ``vision_provider`` is unset (old
    setups keep working). {} when no vision_model is configured."""
    try:
        raw = json.loads((_global_home() / "desktop.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    model = str(raw.get("vision_model") or "").strip()
    if not model:
        return {}
    # Vision's OWN provider (a keyring label) wins; else the main text default.
    label = str(raw.get("vision_provider") or "").strip()
    if label:
        entry = resolve_named_key(label)
        if entry.get("base_url") and entry.get("api_key"):
            return {"model": model, "provider": entry.get("provider", ""),
                    "base_url": entry["base_url"], "api_key": entry["api_key"]}
    provider = str(raw.get("provider") or "").strip()
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    return {"model": model, "provider": provider, "base_url": base_url,
            "api_key": global_api_key(provider, base_url)}


def resolve_named_key(label: str) -> dict[str, str]:
    """Resolve a NAMED keyring entry (~/.chara/desktop.json `keys`) to its
    {provider, base_url, api_key, model}. Used by /provider to switch a chara's
    provider to a saved key. Empty dict when the label is absent or keyless."""
    label = (label or "").strip()
    if not label:
        return {}
    try:
        raw = json.loads((_global_home() / "desktop.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    keys = raw.get("keys") if isinstance(raw, dict) and isinstance(raw.get("keys"), dict) else {}
    item = keys.get(label)
    if not isinstance(item, dict) or not item.get("api_key"):
        return {}
    return {k: str(item.get(k) or "") for k in ("provider", "base_url", "api_key", "model")}


# Real LLM providers that go through the OpenAI-compatible HTTP path.
LIVE_PROVIDERS = {"openai_compatible", "openai", "ollama", "openrouter"}

# The reasoning-effort enum (default "medium"). Mirrored by the SPA's ModelPane
# REASONING; the two are pinned equal by a cross-language drift test.
REASONING_EFFORTS = ("off", "low", "medium", "high")


@dataclass
class Settings:
    """Mutable runtime settings, edited by the welcome screen and persisted to disk."""

    provider: str = "mock"
    base_url: str = ""
    api_key: str = ""
    # No hardcoded default model — it's set at wake (the gate requires one) / in the
    # session config. An empty model surfaces a real error rather than a silent fallback.
    model: str = ""
    temperature: float = 0.85
    # Reply/tool-call token budget. Must be generous: tool-call arguments (e.g. a
    # whole file written via the `terminal` tool) stream inside this budget, and a
    # tiny cap truncates the arguments JSON mid-write → "missing argument" errors.
    max_tokens: int = 4096
    # NOTE: there is deliberately no `lang` setting. Language is not a user choice —
    # it is a property of the active character card (a .zh card speaks zh, a .en card
    # speaks en). The engine and tools are language-agnostic.
    # (There is no py_backend field: the jail is NOT a config.json fact — it's derived
    # from session.json `isolation` via meta.env()→CHARA_PY_BACKEND, the one authority.)
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
    # (AUXILIARY read-image is GLOBAL, not a per-chara field — see
    # global_vision_route below.)
    # Show the thinking TEXT in the transcript (dimmed)? Default off: you get a
    # Claude-style "✶ thinking…" indicator instead, and the text leaves no trace.
    show_thinking: bool = False
    # Interaction mode — how the chara behaves while you are attached (see presence/):
    #   live = greets you, then keeps living its own loop while you watch (default)
    #   chat = greets you, then attends to you only — no self-talk while attached
    # (Detached life is not a mode: `chara start/stop` is that switch.)
    mode: str = "live"
    # Engagement quiet period, seconds: while you are actively talking the chara
    # sets its own work aside; after this much silence it picks its life back up.
    quiet: int = DEFAULT_QUIET
    # Max tool-call iterations in one turn. A chara doing real autonomous work
    # needs room (read → run → write → verify chains); the loop guardrails stop
    # genuinely-stuck repetition, so this can be generous. Operator-configurable.
    max_tool_steps: int = 80
    # Base seconds between spontaneous cycles. Card defaults may
    # supply this; operator commands/env/config persist an override.
    patience: float = DEFAULT_PATIENCE
    # Internal source bit: distinguishes the bare default from an operator
    # intentionally setting the default value, so card defaults can still win
    # when unset.
    patience_override: bool = False
    # Embodiment stance override. Empty means respect the card, then literal.
    # literal = tools are the chara's own hands; actor = tools are backstage.
    embodiment_override: str = ""
    # personal_website module override: "on" | "off" | "" (respect card, then off).
    # The chara keeps a homepage (home/index.html) shown in the website tab; the
    # module adds neutral prompt guidance. Set at wake, editable→next start.
    website_override: str = ""

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
    "character_path": ("CHARA_CHARACTER", "LUNAMOSS_CHARACTER"),
    "user_name": ("CHARA_USER", "LUNAMOSS_USER"),
    "tui_theme_path": ("CHARA_THEME", "LUNAMOSS_THEME"),
    "toolpack": ("CHARA_TOOLPACK", "LUNAMOSS_TOOLPACK"),
    "memory_chars": ("CHARA_MEMORY_CHARS", "LUNAMOSS_MEMORY_CHARS"),
    "user_chars": ("CHARA_USER_CHARS",),
    "mode": ("CHARA_MODE", "CHARA_PRESENCE"),
    "reasoning": ("LLM_REASONING",),
    "patience": ("CHARA_PATIENCE",),
    "embodiment_override": ("CHARA_EMBODIMENT",),
    "website_override": ("CHARA_WEBSITE",),
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
        return v if v in REASONING_EFFORTS else "medium"
    if name in {"show_thinking", "patience_override"}:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if name == "embodiment_override":
        v = str(raw).strip().lower()
        return v if v in {"literal", "actor"} else ""
    if name == "website_override":
        v = str(raw).strip().lower()
        return v if v in {"on", "off"} else ""
    return str(raw)


_log = logging.getLogger("chara.settings")


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
                            if patience_is_explicit(float(data[k])):
                                data["patience_override"] = True
                    except (TypeError, ValueError):
                        pass
        except (json.JSONDecodeError, OSError):
            pass
    # SEC-2: the provider key is resolved from the GLOBAL keyring (env override >
    # keyring), NEVER from config.json — config.json is no longer a key store.
    env_key = os.environ.get("OPENAI_API_KEY") or ""
    legacy_key = str(data.get("api_key") or "")  # a secret still embedded in config.json (pre-keyring)
    provider = str(data.get("provider") or "")
    base_url = str(data.get("base_url") or "")
    # One-time fold: a legacy config.json key (GLOBAL or SESSION) is moved into the
    # keyring so it isn't orphaned when we stop reading config.json — then stripped from
    # disk below. We fold a SESSION key too (not just a global one): the keyring usually
    # already owns it, but if it doesn't (a hand-edited / pre-keyring session dir),
    # skipping the fold AND stripping would destroy the only copy. Best-effort: if the
    # keyring write fails (read-only home), don't fold — and the strip below is gated on
    # a surviving copy, so the key is never lost.
    folded = False
    if legacy_key and not global_api_key(provider, base_url):
        try:
            save_global_key(provider, base_url, legacy_key, model=str(data.get("model") or ""))
            folded = True
        except OSError:
            pass  # keyring not writable — leave the key in config.json rather than orphan it
    global_key = global_api_key(provider, base_url)
    # Resolve: env > keyring > the embedded legacy key. The legacy fallback only bites
    # when the keyring couldn't be written (read-only home), so the chara still works
    # THIS session instead of authing with an empty key — and it's still never written
    # back to config.json (the strip below is gated independently, not on this).
    data["api_key"] = env_key or global_key or legacy_key
    # config.json must never hold the secret (session OR global) — strip it on read, but
    # ONLY once the key is preserved elsewhere (keyring has it, or the env provides it).
    # Stripping with nothing to fall back to would destroy the only copy.
    if legacy_key and (global_key or env_key or folded) and CONFIG_PATH.exists():
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
    # SEC-2: NEVER persist the provider key into config.json — session OR global. The
    # ONE store is the global keyring (write via save_global_key); config.json holds
    # only non-secret route/overrides. This is what kills the second-copy drift.
    data.pop("api_key", None)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return CONFIG_PATH
