"""Character-card market — browse & import open-source SillyTavern cards.

The `market` board section's first tab (`characters`). Today it's a thin, on-demand
proxy to character-tavern.com's PUBLIC catalog API (no upstream key, no scraping —
just its JSON API). The hub fetches directly because it runs on the user's own
machine (unlike a phone, which would need a hosted proxy); a failure surfaces a real
error, never a blank result.

Decoupled by design: this module depends only on stdlib HTTP + the card/avatar
writers (`cards.save_card`, `avatars.*`). It does NOT touch the agent, the deck UI,
or any other subsystem — so a future `skills` market tab slots in beside it without
entangling the rest of the codebase.

Import maps a foreign V2/V3 ST card onto our card shape and is deliberately tolerant
of what a foreign card LACKS: no `polaris` (理想) → the field is omitted (the runtime
already treats an absent aspiration as "none"); no theme color → a deterministic per
-card color is derived (never a primary-less theme, which used to crash the deck);
`{{char}}`/`{{user}}` macros are left intact (the runtime substitutes them at render).
The card's cover art is attached as the keyvisual anchor + avatar, best-effort.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import cards as _cards
from ._common import HubRpcError

_log = logging.getLogger("lunamoth.server.hub.card_market")

# character-tavern.com public catalog API (the same endpoints KokoChat's hosted
# proxy fronts). Overridable for a mirror / self-hosted ST instance.
_SEARCH_ENDPOINT = "https://character-tavern.com/api/search/cards"
_DETAIL_ENDPOINT = "https://character-tavern.com/api/character"
_IMAGE_BASE = "https://cards.character-tavern.com"
_PAGE_BASE = "https://character-tavern.com/character"

_DEFAULT_LIMIT = 24
_MAX_LIMIT = 40
_TIMEOUT_S = 15.0
_NSFW_EXCLUDES = ("nsfw", "explicit", "smut", "porn")
_UA = "lunamoth-card-market/1.0 (+https://lunamoth.ai)"
_DEFAULT_THEME_PRIMARY = "#5B9FD4"  # deck signature blue — the ultimate fallback


# ---- HTTP (stdlib; the hub already does outbound HTTP for model calls) ----------

def _request(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:  # upstream said no (404 unknown card, 5xx, …)
        raise HubRpcError(
            -32050, f"character-tavern returned HTTP {e.code}",
            {"kind": "market", "status": int(e.code)},
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HubRpcError(
            -32050, f"could not reach character-tavern ({type(e).__name__})",
            {"kind": "market", "detail": str(getattr(e, "reason", e))},
        ) from None


def _get_json(url: str) -> dict[str, Any]:
    raw = _request(url)
    try:
        out = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError) as e:
        raise HubRpcError(-32050, "character-tavern returned non-JSON", {"kind": "market"}) from e
    if not isinstance(out, dict):
        raise HubRpcError(-32050, "character-tavern returned an unexpected shape", {"kind": "market"})
    return out


# ---- small value coercions -----------------------------------------------------

def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _arr(v: Any) -> list[str]:
    return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _norm_path(value: Any) -> str:
    """A card path is `<author>/<slug>`; tolerate a full page URL or stray slashes.
    Kept RAW (may contain spaces / unicode) — the API path identity. Encode for URLs."""
    p = _s(value)
    if p.startswith("http"):
        p = p.split("/character/", 1)[-1]
    return p.strip("/")


def _encode_path(path: str) -> str:
    """Percent-encode each path segment for a valid URL — card paths can carry spaces
    or unicode (e.g. `bmboster/Yae Miko`), which would otherwise break the detail fetch
    (urllib) and the <img> URL. The `/` separators are preserved."""
    return "/".join(urllib.parse.quote(seg) for seg in path.split("/"))


# ---- search --------------------------------------------------------------------

def _normalize_hit(raw: Any) -> dict[str, Any] | None:
    """One search hit → a lightweight row for the grid (no full definition)."""
    if not isinstance(raw, dict):
        return None
    path = _norm_path(raw.get("path"))
    if not path:
        return None
    return {
        "path": path,
        "name": _s(raw.get("name")),
        "tagline": _s(raw.get("tagline")),
        "author": _s(raw.get("author")),
        "tags": _arr(raw.get("tags"))[:12],
        "nsfw": raw.get("isNSFW") is True,
        "hasLorebook": raw.get("hasLorebook") is True,
        "imageUrl": f"{_IMAGE_BASE}/{_encode_path(path)}.png",
        "pageUrl": f"{_PAGE_BASE}/{_encode_path(path)}",
        "excerpt": _truncate(_s(raw.get("characterFirstMessage")) or _s(raw.get("pageDescription")), 240),
    }


def search(query: str, *, limit: int = _DEFAULT_LIMIT, nsfw: bool = False) -> dict[str, Any]:
    """Search the open card catalog. Returns lightweight rows; import fetches the full
    definition on demand. A blank query is an explicit error (no point hitting upstream)."""
    q = _s(query)
    if not q:
        raise HubRpcError(-32602, "a search query is required", {"kind": "market"})
    n = max(1, min(_MAX_LIMIT, int(limit or _DEFAULT_LIMIT)))
    params = [("query", q), ("sort", "most_popular"), ("limit", str(n))]
    if not nsfw:
        params.append(("exclude_tags", ",".join(_NSFW_EXCLUDES)))
    url = f"{_SEARCH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    payload = _get_json(url)
    hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
    candidates = [h for h in (_normalize_hit(x) for x in hits) if h is not None]
    total = payload.get("totalHits")
    return {
        "query": q,
        "candidates": candidates,
        "totalHits": int(total) if isinstance(total, (int, float)) else len(candidates),
    }


# ---- theme derivation (deterministic per card, never primary-less) --------------

def _seeded_theme(seed: str) -> dict[str, str]:
    """A stable, pleasant {primary, secondary} derived from the card identity, so an
    imported card (which carries no theme) still gets a distinct, valid color — not the
    same flat blue for every import, and never a missing primary."""
    if not seed:
        return {"primary": _DEFAULT_THEME_PRIMARY, "secondary": ""}
    h = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16)
    hue = (h % 360) / 360.0
    primary = _hex(colorsys.hls_to_rgb(hue, 0.60, 0.55))
    secondary = _hex(colorsys.hls_to_rgb((hue + 35 / 360.0) % 1.0, 0.55, 0.50))
    return {"primary": primary, "secondary": secondary}


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


# ---- import (foreign ST card → our card shape) ---------------------------------

def _map_to_card(detail: dict[str, Any]) -> dict[str, Any]:
    """Map a character-tavern `/api/character` card onto our V3 card object.

    The upstream card carries the persona as flat ``definition_*`` fields at the TOP
    level (plus `path`, `name`, `tagline`, `character_book`, …) — NOT a nested `data`
    block. We fold those into our `data`; `character_book` (lorebook) passes through to
    our embedded world; LunaMoth-only extensions are filled with sane defaults or
    omitted. Result matches `cards.save_card`'s `{version, name, data:{…, extensions}}`."""
    path = _norm_path(detail.get("path"))
    name = _s(detail.get("inChatName")) or _s(detail.get("name")) or (path.split("/")[-1] if path else "")
    tagline = _s(detail.get("tagline"))
    author = _s(detail.get("author")) or (path.split("/")[0] if "/" in path else "")

    ext: dict[str, Any] = {
        # theme ALWAYS present (deck-crash guard); foreign cards carry no color, so derive one.
        "theme": _seeded_theme(path or name),
        # provenance — we proxy/link, never claim authorship.
        "source": "character_tavern",
        "source_path": path,
        "source_url": f"{_PAGE_BASE}/{_encode_path(path)}" if path else "",
        # the cover URL, preserved so the UI can show it browser-side even if the
        # server can't download the bytes (the CDN hotlink-protects server fetches).
        "source_image": f"{_IMAGE_BASE}/{_encode_path(path)}.png" if path else "",
    }
    if tagline:
        ext["tagline"] = tagline
    # NOTE: no `polaris` (理想) — it's the USER's north-star, never imported. Absent is
    # safe: the runtime injects no aspiration block when the field is missing.

    data: dict[str, Any] = {
        "name": name,
        "description": _s(detail.get("definition_character_description")),
        "personality": _s(detail.get("definition_personality")),
        "scenario": _s(detail.get("definition_scenario")),
        "first_mes": _s(detail.get("definition_first_message")),
        "mes_example": _s(detail.get("definition_example_messages")),
        "system_prompt": _s(detail.get("definition_system_prompt")),
        "post_history_instructions": _s(detail.get("definition_post_history_prompt")),
        "alternate_greetings": _arr(detail.get("alternate_greetings"))[:8],
        "creator_notes": _s(detail.get("creator_notes")) or tagline,
        "creator": author or "character-tavern.com",
        "character_version": "1.0",
        "tags": _arr(detail.get("tags"))[:24],
        "extensions": {"lunamoth": ext},
    }
    book = detail.get("character_book")
    if isinstance(book, dict) and isinstance(book.get("entries"), list) and book["entries"]:
        data["character_book"] = book  # embedded world — our two-tier worldinfo reads it as-is
    return {"version": "1.0", "name": name, "data": data}


def import_card(path: str, *, nsfw: bool = False) -> dict[str, Any]:
    """Fetch a card's full definition and write it into the user deck. Returns the new deck
    card path + the cover URL. The imported card lands UNLOCKED (a template) — the user
    reviews/edits it and wakes it like any deck card.

    The COVER is brought over CLIENT-SIDE, not here: character-tavern's image CDN
    hotlink-protects against non-browser fetches (a datacenter-hosted hub gets 403), so
    the only reliable "real client" is the user's browser. The web client fetches the
    cover and uploads it via card.asset_save/card.avatar_upload after this returns. We
    surface `image_url` so it can; the card also stores it (extensions.source_image)."""
    p = _norm_path(path)
    if not p:
        raise HubRpcError(-32602, "a card path is required", {"kind": "market"})
    payload = _get_json(f"{_DETAIL_ENDPOINT}/{_encode_path(p)}")
    detail = payload.get("card") if isinstance(payload.get("card"), dict) else payload
    has_identity = isinstance(detail, dict) and (
        _s(detail.get("name")) or _s(detail.get("inChatName")) or _s(detail.get("definition_character_description"))
    )
    if not has_identity:
        raise HubRpcError(-32050, "character-tavern returned no usable card definition", {"kind": "market"})
    if not nsfw and detail.get("isNSFW") is True:
        raise HubRpcError(-32602, "this card is marked NSFW; enable NSFW to import it", {"kind": "market"})
    card = _map_to_card(detail)
    saved = _cards.save_card(card)  # writes into the user deck, returns {"path": ...}
    card_path = str(saved.get("path") or "")
    image_url = f"{_IMAGE_BASE}/{_encode_path(p)}.png"
    return {"path": card_path, "name": card["name"], "image_url": image_url, "source_path": p}
