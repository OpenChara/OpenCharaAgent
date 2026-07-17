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

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ...content.cards import normalize_character_book
from . import cards as _cards
from ._common import SEEDED_THEME_DEFAULT, HubRpcError, seeded_theme

_log = logging.getLogger("chara.server.hub.card_market")

# character-tavern.com public catalog API (the same endpoints KokoChat's hosted
# proxy fronts). Overridable for a mirror / self-hosted ST instance.
_SEARCH_ENDPOINT = "https://character-tavern.com/api/search/cards"
_DETAIL_ENDPOINT = "https://character-tavern.com/api/character"
# The cover host is the STORAGE CDN (serves any client, supports on-the-fly resize via
# ?width/quality/format) — NOT cards.character-tavern.com, which hotlink-403s everyone.
_IMAGE_BASE = "https://ct-cards.storage.character-tavern.com"
_PAGE_BASE = "https://character-tavern.com/character"

_DEFAULT_LIMIT = 24
_MAX_LIMIT = 48
_TIMEOUT_S = 15.0
_NSFW_EXCLUDES = ("nsfw", "explicit", "smut", "porn")
# The upstream honours these distinct sorts; everything else aliases to most_popular.
_VALID_SORTS = ("most_popular", "trending", "newest")
_THUMB_WIDTH = 400   # grid card cover (resized → ~10% of raw bytes)
_PREVIEW_WIDTH = 640  # detail-view cover
_UA = "chara-card-market/1.0 (+https://lunamoth.ai)"
# Deterministic per-card theme lives in _common (shared with the paste-import path);
# alias the names this module + its tests use.
_DEFAULT_THEME_PRIMARY = SEEDED_THEME_DEFAULT
_seeded_theme = seeded_theme


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
    Kept RAW (may contain spaces / unicode) — the API path identity. Encode for URLs.
    Traversal segments (`.`/`..`) are rejected → '' (the host is hard-coded so this is
    hygiene, not an SSRF/write fix, but a card path never legitimately contains them)."""
    p = _s(value)
    if p.startswith("http"):
        p = p.split("/character/", 1)[-1]
    p = p.strip("/")
    if not p or any(seg in ("", ".", "..") for seg in p.split("/")):
        return ""
    return p


def _encode_path(path: str) -> str:
    """Percent-encode each path segment for a valid URL — card paths can carry spaces
    or unicode (e.g. `bmboster/Yae Miko`), which would otherwise break the detail fetch
    (urllib) and the <img> URL. The `/` separators are preserved."""
    return "/".join(urllib.parse.quote(seg) for seg in path.split("/"))


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _image_url(path: str, *, width: int = 0) -> str:
    """A card's cover on the storage CDN. With `width` it's a resized/auto-format thumb
    (small, fast); without, the full-res original (used as the imported sprite source)."""
    if not path:
        return ""
    base = f"{_IMAGE_BASE}/{_encode_path(path)}.png"
    return f"{base}?width={width}&quality=82&format=auto" if width else base


# ---- search --------------------------------------------------------------------

def _normalize_hit(raw: Any) -> dict[str, Any] | None:
    """One search hit → a row for the grid (no full definition; the persona is fetched
    on demand by `detail`). Carries the ranking signals the upstream exposes so the grid
    can show download/like badges."""
    if not isinstance(raw, dict):
        return None
    path = _norm_path(raw.get("path"))
    if not path:
        return None
    return {
        "path": path,
        "name": _s(raw.get("inChatName")) or _s(raw.get("name")),
        "tagline": _s(raw.get("tagline")),
        "author": _s(raw.get("author")),
        "tags": _arr(raw.get("tags"))[:12],
        "nsfw": raw.get("isNSFW") is True,
        "hasLorebook": raw.get("hasLorebook") is True,
        "oc": raw.get("isOC") is True,
        "downloads": _int(raw.get("downloads")),
        "likes": _int(raw.get("likes")),
        "messages": _int(raw.get("messages")),
        "imageUrl": _image_url(path, width=_THUMB_WIDTH),
        "pageUrl": f"{_PAGE_BASE}/{_encode_path(path)}",
        "excerpt": _truncate(_s(raw.get("characterFirstMessage")) or _s(raw.get("pageDescription")), 240),
    }


def search(query: str = "", *, sort: str = "most_popular", limit: int = _DEFAULT_LIMIT,
           page: int = 1, nsfw: bool = False, tags: Any = None,
           oc: bool = False, lorebook: bool = False) -> dict[str, Any]:
    """Browse / search the open card catalog. The query is OPTIONAL — an empty query with
    a sort is the default browse (the popularity ranking / trending / newest), so the
    market opens to real content, not a blank box. Filters: `tags` (one or more), `oc`
    (original characters), `lorebook` (carries a world book), `nsfw` (default excludes it).
    Paged via `page`/`totalPages`. Returns grid rows; `detail` fetches a card's persona."""
    q = _s(query)
    sort = sort if sort in _VALID_SORTS else "most_popular"
    n = max(1, min(_MAX_LIMIT, int(limit or _DEFAULT_LIMIT)))
    pg = max(1, _int(page) or 1)
    params: list[tuple[str, str]] = [("sort", sort), ("limit", str(n)), ("page", str(pg))]
    if q:
        params.append(("query", q))
    tag_list = [tags] if isinstance(tags, str) else (tags if isinstance(tags, list) else [])
    # multiple tags AND via ONE comma-joined param (repeated `tags=` only honours the first).
    tags_joined = ",".join(t for t in (_s(x) for x in tag_list) if t)
    if tags_joined:
        params.append(("tags", tags_joined))
    if oc:
        params.append(("isOC", "true"))
    if lorebook:
        params.append(("hasLorebook", "true"))
    if not nsfw:
        params.append(("exclude_tags", ",".join(_NSFW_EXCLUDES)))
    url = f"{_SEARCH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    payload = _get_json(url)
    hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
    candidates = [h for h in (_normalize_hit(x) for x in hits) if h is not None]
    total = payload.get("totalHits")
    return {
        "query": q,
        "sort": sort,
        "page": pg,
        "totalPages": max(1, _int(payload.get("totalPages")) or 1),
        "candidates": candidates,
        "totalHits": int(total) if isinstance(total, (int, float)) else len(candidates),
    }


def detail(path: str) -> dict[str, Any]:
    """A card's full persona for the preview view (read-only browse before importing) —
    the same `/api/character` fetch `import_card` uses, normalized for display. No write."""
    p = _norm_path(path)
    if not p:
        raise HubRpcError(-32602, "a card path is required", {"kind": "market"})
    payload = _get_json(f"{_DETAIL_ENDPOINT}/{_encode_path(p)}")
    d = payload.get("card") if isinstance(payload.get("card"), dict) else payload
    if not isinstance(d, dict) or not (
        _s(d.get("name")) or _s(d.get("inChatName")) or _s(d.get("definition_character_description"))
    ):
        raise HubRpcError(-32050, "character-tavern returned no usable card definition", {"kind": "market"})
    tags = _arr(d.get("tags"))[:24]
    nsfw = d.get("isNSFW") is True or bool({t.lower() for t in tags} & set(_NSFW_EXCLUDES))
    return {
        "path": p,
        "name": _s(d.get("inChatName")) or _s(d.get("name")),
        "author": _s(d.get("author")) or (p.split("/")[0] if "/" in p else ""),
        "tagline": _s(d.get("tagline")),
        "description": _s(d.get("definition_character_description")),
        "personality": _s(d.get("definition_personality")),
        "scenario": _s(d.get("definition_scenario")),
        "first_mes": _s(d.get("definition_first_message")),
        "mes_example": _s(d.get("definition_example_messages")),
        "tags": tags,
        "nsfw": nsfw,
        "hasLorebook": bool(d.get("lorebookId")) or d.get("hasLorebook") is True,
        "oc": d.get("isOC") is True,
        "downloads": _int(d.get("analytics_downloads")),
        "views": _int(d.get("analytics_views")),
        "messages": _int(d.get("analytics_messages")),
        "imageUrl": _image_url(p, width=_PREVIEW_WIDTH),
        "pageUrl": f"{_PAGE_BASE}/{_encode_path(p)}",
    }


# ---- import (foreign ST card → our card shape) ---------------------------------

def _map_to_card(detail: dict[str, Any]) -> dict[str, Any]:
    """Map a character-tavern `/api/character` card onto our V3 card object.

    The upstream card carries the persona as flat ``definition_*`` fields at the TOP
    level (plus `path`, `name`, `tagline`, `character_book`, …) — NOT a nested `data`
    block. We fold those into our `data`; `character_book` (lorebook) passes through to
    our embedded world; OpenCharaAgent-only extensions are filled with sane defaults or
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
        # the full-res cover URL, preserved as the sprite/立绘 display fallback (and the
        # source the client fetches to store a local keyvisual+sprite).
        "source_image": _image_url(path),
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
        "extensions": {"chara": ext},
    }
    book = normalize_character_book(detail.get("character_book"))
    if book is not None:
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
    # NSFW gate: trust the explicit flag AND the tags — the detail payload doesn't always
    # echo `isNSFW`, so a tag match (nsfw/explicit/…) is the backstop that keeps a direct
    # import-by-path from slipping past the search-side `exclude_tags`.
    detail_tags = {t.lower() for t in _arr(detail.get("tags"))}
    is_nsfw = detail.get("isNSFW") is True or bool(detail_tags & set(_NSFW_EXCLUDES))
    if not nsfw and is_nsfw:
        raise HubRpcError(-32602, "this card is marked NSFW; enable NSFW to import it", {"kind": "market"})
    card = _map_to_card(detail)
    saved = _cards.save_card(card)  # writes into the user deck, returns {"path": ...}
    card_path = str(saved.get("path") or "")
    return {"path": card_path, "name": card["name"], "image_url": _image_url(p), "source_path": p}
