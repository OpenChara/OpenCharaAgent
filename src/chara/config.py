from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_ROOT = Path(os.getenv("CHARA_SANDBOX", ROOT / "sandbox")).resolve()

# OpenRouter app attribution. When a request goes to openrouter.ai we send these
# two headers so all OpenCharaAgent usage groups under one app on openrouter.ai/apps
# and in the per-key Activity view. OpenRouter derives the app's display NAME from
# X-Title and its ICON from the favicon of the HTTP-Referer URL — so to show a
# OpenCharaAgent icon (not GitHub's), point the referer at a public page that serves the
# OpenCharaAgent favicon. Both are env-overridable for a deployer's own site, no code edit.
OPENROUTER_REFERER = os.getenv("CHARA_OPENROUTER_REFERER", "https://lunamoth.ai/")
OPENROUTER_TITLE = os.getenv("CHARA_OPENROUTER_TITLE", "OpenCharaAgent")


def openrouter_attribution_headers(base_url: str | None) -> dict[str, str]:
    """The HTTP-Referer / X-Title attribution headers, but only when the request
    targets OpenRouter (harmless elsewhere, but we keep them scoped). Shared by the
    chat path (core/llm.py), the hub's auxiliary completions (server/hub/models.py),
    and the OpenRouter image adapter (tools/builtin/_image_gen.py) so EVERY token of
    OpenCharaAgent usage is attributed to the same app — same name, same icon."""
    if "openrouter.ai" in (base_url or ""):
        return {"HTTP-Referer": OPENROUTER_REFERER, "X-Title": OPENROUTER_TITLE}
    return {}


# ---- credential prefixes -----------------------------------------------------
# The ONE list of known API-key prefixes. THREE redactors build their own regex
# from it — the wire/URL exfil guard (tools/builtin/_url_safety, shared by browser),
# the at-rest redactor (core/redact, for transcripts + request logs), and the disk
# log scrubber (obs/log). They live in three layers that can't import each other,
# but ALL may import config (the leaf root) — so a new prefix added HERE propagates
# to every redactor instead of leaking through whichever copy was missed.
SECRET_PREFIX_PATTERNS: tuple[str, ...] = (
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth access token
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server token
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server token
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack tokens
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",           # Perplexity
    r"fal_[A-Za-z0-9_-]{10,}",          # Fal.ai
    r"fc-[A-Za-z0-9]{10,}",             # Firecrawl
    r"bb_live_[A-Za-z0-9_-]{10,}",      # BrowserBase
    r"gAAAA[A-Za-z0-9_=-]{20,}",        # Codex encrypted tokens
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe secret key (live)
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe secret key (test)
    r"rk_live_[A-Za-z0-9]{10,}",        # Stripe restricted key
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid API key
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace token
    r"r8_[A-Za-z0-9]{10,}",             # Replicate API token
    r"npm_[A-Za-z0-9]{10,}",            # npm access token
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI API token
    r"dop_v1_[A-Za-z0-9]{10,}",         # DigitalOcean PAT
    r"doo_v1_[A-Za-z0-9]{10,}",         # DigitalOcean OAuth
    r"am_[A-Za-z0-9_-]{10,}",           # AgentMail API key
    r"sk_[A-Za-z0-9_]{10,}",            # ElevenLabs TTS key (sk_ underscore)
    r"tvly-[A-Za-z0-9]{10,}",           # Tavily search API key
    r"exa_[A-Za-z0-9]{10,}",            # Exa search API key
    r"gsk_[A-Za-z0-9]{10,}",            # Groq Cloud API key
    r"syt_[A-Za-z0-9]{10,}",            # Matrix access token
    r"retaindb_[A-Za-z0-9]{10,}",       # RetainDB API key
    r"hsk-[A-Za-z0-9]{10,}",            # Hindsight API key
    r"mem0_[A-Za-z0-9]{10,}",           # Mem0 Platform API key
    r"brv_[A-Za-z0-9]{10,}",            # ByteRover API key
    r"xai-[A-Za-z0-9]{30,}",            # xAI (Grok) API key
)


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """Write *text* to *path* atomically (temp file in the same dir + ``os.replace``)
    so a crash mid-write can never truncate a config/state file, and a concurrent
    reader sees either the whole old or whole new file. ``private`` chmods 0600 (for
    files that may hold secrets). The leaf helper for ``session/`` + ``core/state``;
    the hub keeps its own copy in server/hub/_common."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if private:
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def find_uv() -> "str | None":
    """Locate the ``uv`` binary robustly — the ONE resolver every uv caller uses.

    OpenCharaAgent is uv-based (install.sh drops uv in ~/.chara/bin), but a desktop /
    Electron launch does NOT inherit the shell PATH, so ``shutil.which`` alone misses
    it — a real cause of 'uv not found' in the update + matte installers. Fall back to
    the known install locations. Returns None only if uv is genuinely absent."""
    import shutil

    found = shutil.which("uv")
    if found:
        return found
    home = Path(os.getenv("CHARA_HOME") or (Path.home() / ".chara"))
    for p in (home / "bin" / "uv",
              Path.home() / ".local" / "bin" / "uv",
              Path.home() / ".cargo" / "bin" / "uv"):
        if p.exists():
            return str(p)
    return None


def content_dir(name: str) -> Path:
    """Resolve a bundled-content dir (``cards`` / ``toolpacks``).

    In a dev checkout these live at the repo root (``ROOT/<name>``). A WHEEL
    install has no repo root — ``ROOT`` points into site-packages — so the build
    (`scripts/build-wheel.sh`) copies them into ``chara/_bundled/<name>``,
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
    model: str = os.getenv("OPENAI_MODEL", "")  # no hardcoded default — the chara's model is configured (no fallback model)
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.85"))
    # Max OUTPUT tokens. 0 = AUTO (the default): follow the model — providers.py
    # resolves the model's real max_completion_tokens (OpenRouter), falling back
    # to 8192 (hermes' default). A flat 4096 used to cut large write_file/patch
    # tool-call args mid-argument (~12KB). Set >0 to force an explicit cap.
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "0"))
    # Reasoning effort for thinking models: off | low | medium | high.
    # Default ON at medium; only sent to routes/models known to accept it.
    reasoning: str = os.getenv("LLM_REASONING", "medium").strip().lower()
    # Vision is a model CAPABILITY, not a preference — auto-detected by name.
    # `on`/`off` is a safety valve for routes the name heuristic can't read
    # (a custom-named vision model, or a text-only one that fakes a vision name).
    vision: str = os.getenv("LLM_VISION", "auto").strip().lower()
    # (AUXILIARY vision is GLOBAL now — session.settings.global_vision_route resolves
    # the read-image model + its OWN provider from desktop.json, not a per-chara field,
    # so a per-chara provider switch can't break image reading.)
    # Anthropic prompt-cache TTL tier: "5m" (default) or "1h". 1h costs ~2x on
    # write vs 1.25x for 5m but amortizes across long sessions with >5-min gaps
    # between turns. Only applied on Anthropic-family routes (see core/cache.py).
    cache_ttl: str = os.getenv("LLM_CACHE_TTL", "5m").strip().lower()


@dataclass(frozen=True)
class ThoughtConfig:
    use_llm: bool = os.getenv("THOUGHT_USE_LLM", "true").strip().lower() in {"1", "true", "yes", "on"}
