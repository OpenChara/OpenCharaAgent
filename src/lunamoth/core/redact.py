"""Regex-based secret redaction — a code-level backstop for the compaction
summary (and any other text that must not carry raw credentials).

The compaction summarizer is *told* to write [REDACTED] for secrets, but a
model can forget; this scrubs known credential shapes from the summary INPUT
and OUTPUT so a leaked key never persists into the context even if the model
slips. Pure regex, no model call, non-matching text passes through unchanged.

Ported apple-to-apple from the upstream reference's redact.py (the active
patterns; the URL-query/userinfo redactors are intentionally OFF there too, as
opaque tokens in query strings are often legitimate workflow data). Short
tokens (< 18 chars) are fully masked; longer ones keep 6 prefix / 4 suffix for
debuggability.

ON by default; opt out with LUNAMOTH_REDACT_SECRETS=false (snapshotted at
import so a mid-session env mutation can't silently disable it).
"""
from __future__ import annotations

import os
import re

# Snapshot at import time so a runtime env change can't disable redaction mid-run.
_REDACT_ENABLED = os.getenv("LUNAMOTH_REDACT_SECRETS", "true").strip().lower() in {"1", "true", "yes", "on"}

# Known API-key prefixes — match the prefix + contiguous token chars.
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",           # Perplexity
    r"fal_[A-Za-z0-9_-]{10,}",          # Fal.ai
    r"fc-[A-Za-z0-9]{10,}",             # Firecrawl
    r"gAAAA[A-Za-z0-9_=-]{20,}",        # encrypted tokens
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe live
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe test
    r"rk_live_[A-Za-z0-9]{10,}",        # Stripe restricted
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace
    r"r8_[A-Za-z0-9]{10,}",             # Replicate
    r"npm_[A-Za-z0-9]{10,}",            # npm
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI
    r"dop_v1_[A-Za-z0-9]{10,}",         # DigitalOcean PAT
    r"gsk_[A-Za-z0-9]{10,}",            # Groq
    r"tvly-[A-Za-z0-9]{10,}",           # Tavily
    r"exa_[A-Za-z0-9]{10,}",            # Exa
    r"xai-[A-Za-z0-9]{30,}",            # xAI (Grok)
]

_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_ENV_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+)\2",
)

_JSON_KEY_NAMES = (
    r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|"
    r"bearer|secret_value|raw_secret|secret_input|key_material)"
)
_JSON_FIELD_RE = re.compile(rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"', re.IGNORECASE)

_AUTH_HEADER_RE = re.compile(r"(Authorization:\s*Bearer\s+)(\S+)", re.IGNORECASE)
_TELEGRAM_RE = re.compile(r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}")
_SIGNAL_PHONE_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")
_FORM_BODY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*(?:&[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*)+$"
)
_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])")

_SENSITIVE_QUERY_PARAMS = frozenset({
    "access_token", "refresh_token", "id_token", "token", "api_key", "apikey",
    "client_secret", "password", "auth", "jwt", "session", "secret", "key",
    "code", "signature", "x-amz-signature",
})


def mask_secret(value: str, *, head: int = 4, tail: int = 4, floor: int = 12,
                placeholder: str = "***", empty: str = "") -> str:
    """Mask a secret for display, keeping ``head`` prefix + ``tail`` suffix chars;
    values shorter than ``floor`` are fully masked."""
    if not value:
        return empty
    if len(value) < floor:
        return placeholder
    return f"{value[:head]}...{value[-tail:]}"


def _mask_token(token: str) -> str:
    """Mask a token — 18-char floor, preserves 6 prefix / 4 suffix."""
    if not token:
        return "***"
    return mask_secret(token, head=6, tail=4, floor=18)


def _extract_literal_prefix(pattern: str) -> str:
    """Leading literal chars of a regex (up to the first metachar) — the substring
    any match MUST contain, for the cheap pre-screen (no false negatives)."""
    meta = "[(\\.?*+|{^$"
    for i, ch in enumerate(pattern):
        if ch in meta:
            return pattern[:i]
    return pattern


_PREFIX_SUBSTRINGS = tuple(_extract_literal_prefix(p) for p in _PREFIX_PATTERNS)


def _has_known_prefix_substring(text: str) -> bool:
    return any(p in text for p in _PREFIX_SUBSTRINGS)


def _redact_query_string(query: str) -> str:
    if not query:
        return query
    parts = []
    for pair in query.split("&"):
        if "=" not in pair:
            parts.append(pair)
            continue
        key, _, _value = pair.partition("=")
        parts.append(f"{key}=***" if key.lower() in _SENSITIVE_QUERY_PARAMS else pair)
    return "&".join(parts)


def _redact_form_body(text: str) -> str:
    """Redact a pure form-urlencoded body (only triggers on clean k=v&k=v)."""
    if not text or "\n" in text or "&" not in text:
        return text
    if not _FORM_BODY_RE.match(text.strip()):
        return text
    return _redact_query_string(text.strip())


def redact_sensitive_text(text, *, force: bool = False, code_file: bool = False):
    """Mask known credential shapes (API keys, ENV/JSON secrets, auth headers,
    Telegram tokens, private keys, DB connstring passwords, JWTs, phone numbers).
    Non-matching text passes through unchanged. ``force=True`` redacts even when
    globally disabled (a safety boundary that must never emit raw secrets).
    ``code_file=True`` skips the ENV/JSON patterns (source-code false positives).

    Each pattern is gated behind a cheap substring pre-check, so the common
    no-secret case skips the regex. (Apple-to-apple with the upstream reference;
    URL query/userinfo redaction is intentionally OFF — opaque query tokens are
    often legitimate workflow data, and credential shapes inside URLs are still
    caught by the prefix/JWT/DB patterns.)"""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text or not (force or _REDACT_ENABLED):
        return text

    if _has_known_prefix_substring(text):
        text = _PREFIX_RE.sub(lambda m: _mask_token(m.group(1)), text)

    if not code_file:
        if "=" in text:
            text = _ENV_ASSIGN_RE.sub(
                lambda m: f"{m.group(1)}={m.group(2)}{_mask_token(m.group(3))}{m.group(2)}", text)
        if ":" in text and '"' in text:
            text = _JSON_FIELD_RE.sub(lambda m: f'{m.group(1)}: "{_mask_token(m.group(2))}"', text)

    if "uthorization" in text or "UTHORIZATION" in text:
        text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + _mask_token(m.group(2)), text)

    if ":" in text:
        text = _TELEGRAM_RE.sub(lambda m: f"{m.group(1) or ''}{m.group(2)}:***", text)

    if "BEGIN" in text and "-----" in text:
        text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)

    if "://" in text:
        text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)

    if "eyJ" in text:
        text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)

    if "&" in text and "=" in text:
        text = _redact_form_body(text)

    if "+" in text:
        def _redact_phone(m):
            phone = m.group(1)
            return (phone[:2] + "****" + phone[-2:]) if len(phone) <= 8 else (phone[:4] + "****" + phone[-4:])
        text = _SIGNAL_PHONE_RE.sub(_redact_phone, text)

    return text
