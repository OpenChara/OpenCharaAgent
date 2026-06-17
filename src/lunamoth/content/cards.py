from __future__ import annotations

import base64
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .worldinfo import Lorebook, apply_macros, entry_to_book_dict

# CJK Unified Ideographs (U+4E00–U+9FFF) — i.e. "does this text contain Han characters".
_CJK = re.compile(r"[\u4e00-\u9fff]")


_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _clean_hex(value: Any) -> str:
    """An #RRGGBB color (upper-cased), or '' — presentation only, never raises."""
    if isinstance(value, str) and _HEX_RE.match(value.strip()):
        return value.strip().upper()
    return ""


def detect_language(source_path: str = "", text: str = "") -> str:
    """A card's language comes from the card, not a user toggle.

    Filename hint first (`*.zh.json` / `*.en.png` etc.), then CJK content ratio.
    """
    stem = Path(source_path).stem.lower() if source_path else ""
    for suffix in (".zh", "-zh", "_zh", ".cn", "-cn"):
        if stem.endswith(suffix):
            return "zh"
    for suffix in (".en", "-en", "_en"):
        if stem.endswith(suffix):
            return "en"
    if text:
        cjk = len(_CJK.findall(text))
        if cjk and cjk / max(1, len(text)) > 0.03:
            return "zh"
    return "en"


def _read_png_text_chunks(path: Path) -> dict[str, bytes]:
    """Return tEXt/iTXt keyword -> value bytes from a PNG (character cards live here)."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    out: dict[str, bytes] = {}
    i = 8
    n = len(data)
    while i + 8 <= n:
        length = struct.unpack(">I", data[i : i + 4])[0]
        ctype = data[i + 4 : i + 8].decode("latin1")
        body = data[i + 8 : i + 8 + length]
        if ctype == "tEXt":
            kw, _, val = body.partition(b"\x00")
            out.setdefault(kw.decode("latin1"), val)
        elif ctype == "iTXt":
            # keyword \0 compflag compmethod \0 lang \0 transkw \0 text
            kw, _, rest = body.partition(b"\x00")
            parts = rest.split(b"\x00", 3)
            if len(parts) == 4:
                out.setdefault(kw.decode("latin1"), parts[3])
        i += 12 + length
        if ctype == "IEND":
            break
    return out


def _decode_card_chunk(raw: bytes) -> dict[str, Any]:
    text = base64.b64decode(raw).decode("utf-8", errors="replace")
    return json.loads(text)


def _card_json_from_png(path: Path) -> dict[str, Any]:
    chunks = _read_png_text_chunks(path)
    # Prefer V3 (ccv3) then V2 (chara).
    for key in ("ccv3", "chara"):
        if key in chunks:
            return _decode_card_chunk(chunks[key])
    raise ValueError("no embedded character card (chara/ccv3) found in PNG")


@dataclass
class CharacterCard:
    name: str = "Character"
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    alternate_greetings: list[str] = field(default_factory=list)
    creator_notes: str = ""
    tags: list[str] = field(default_factory=list)
    character_book: Lorebook | None = None
    extensions: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    @classmethod
    def from_card_dict(cls, card: dict[str, Any], source_path: str = "") -> "CharacterCard":
        # V2/V3 nest the real fields under "data"; V1 is flat.
        data = card.get("data") if isinstance(card.get("data"), dict) else card
        book = None
        cb = data.get("character_book")
        if isinstance(cb, dict) and cb.get("entries"):
            book = Lorebook.from_dict(cb, name=cb.get("name", ""))
        return cls(
            name=str(data.get("name") or card.get("name") or "Character"),
            description=str(data.get("description", "")),
            personality=str(data.get("personality", "")),
            scenario=str(data.get("scenario", "")),
            first_mes=str(data.get("first_mes", "")),
            mes_example=str(data.get("mes_example", "")),
            system_prompt=str(data.get("system_prompt", "")),
            post_history_instructions=str(data.get("post_history_instructions", "")),
            alternate_greetings=list(data.get("alternate_greetings", []) or []),
            creator_notes=str(data.get("creator_notes", "")),
            tags=[str(t) for t in (data.get("tags", []) or [])],
            character_book=book,
            extensions=dict(data.get("extensions", {}) or {}),
            source_path=source_path,
        )

    @classmethod
    def load(cls, path: str | Path) -> "CharacterCard":
        p = Path(path)
        if p.suffix.lower() == ".png":
            card = _card_json_from_png(p)
        else:
            card = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_card_dict(card, source_path=str(p))

    @property
    def language(self) -> str:
        """zh or en, derived from the card itself (filename hint, then content)."""
        sample = " ".join((self.description, self.personality, self.first_mes, self.scenario))[:4000]
        return detect_language(self.source_path, sample)

    def defaults(self) -> dict[str, Any]:
        """The card's recommended tool pack / limits / life knobs.

        Lives in `extensions.lunamoth` (SillyTavern-compatible free-form field):
            {"toolpack": "sandbox", "memory_chars": 8000, "wishes": ["..."]}
        The world is NOT here — it lives in the standard `character_book` field
        (the card is the ONE external file). The context window is NOT here
        either — it's the model's real window (providers.py). Cards that omit
        this block (e.g. plain SillyTavern imports) just get the global
        fallbacks — so any imported card Just Works.

        Seed wishes are read from `extensions.lunamoth.wishes` first, falling
        back to the legacy `extensions.lunamoth.goals` (one-load migration —
        an existing card's seeds are never lost). The normalized list is
        exposed under `wishes`.
        """
        ext = self.extensions.get("lunamoth")
        if not isinstance(ext, dict):
            return {}
        out = dict(ext)
        # wishes (new) first, then legacy goals — never lose an existing card's seeds.
        raw = out.get("wishes")
        if not isinstance(raw, list):
            raw = out.get("goals")
        out.pop("goals", None)
        if isinstance(raw, list):
            out["wishes"] = [str(g).strip() for g in raw if str(g).strip()]
        else:
            out.pop("wishes", None)
        return out

    def theme_colors(self) -> dict[str, str]:
        """The card's dual theme `{primary, secondary}` (presentation, not soul).

        Lives in `extensions.lunamoth.theme = {"primary": "#RRGGBB",
        "secondary": "#RRGGBB"}`. Back-compat: a card carrying only the legacy
        single `theme_color` is read as `{primary: that, secondary: ""}`. Bad
        or missing values come back empty (the renderer falls back to a glyph
        palette). Never raises — a malformed theme must not break a card.
        """
        ext = self.extensions.get("lunamoth")
        if not isinstance(ext, dict):
            return {"primary": "", "secondary": ""}
        primary = ""
        secondary = ""
        theme = ext.get("theme")
        if isinstance(theme, dict):
            primary = _clean_hex(theme.get("primary"))
            secondary = _clean_hex(theme.get("secondary"))
        if not primary:
            primary = _clean_hex(ext.get("theme_color"))
        return {"primary": primary, "secondary": secondary}

    def avatar_file(self) -> str:
        """Relative filename of the avatar sidecar, or '' (presentation field).

        The avatar is a separate file beside the card (png/jpg/jpeg/svg);
        `extensions.lunamoth.avatar_file` holds its name. Traversal-bearing
        values (path separators / parent refs) are refused — the sidecar must
        sit in the card's own directory.
        """
        ext = self.extensions.get("lunamoth")
        if not isinstance(ext, dict):
            return ""
        val = ext.get("avatar_file")
        if not isinstance(val, str):
            return ""
        name = val.strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return ""
        return name

    def avatar_path(self) -> Path | None:
        """The resolved sidecar path if it exists on disk, else None."""
        name = self.avatar_file()
        if not name or not self.source_path:
            return None
        p = Path(self.source_path).parent / name
        return p if p.is_file() else None

    # ---- art-asset library (sprite / background / keyvisual / stickers) ----------
    # Presentation, not soul: optional sidecars living in the card's own folder,
    # declared under extensions.lunamoth.assets. Resolution is traversal-safe and
    # confined to the card directory (so a per-character folder can hold a media set).
    _ASSET_KINDS = ("sprite", "background", "keyvisual")

    def _asset_rel(self, value) -> str:
        """A safe card-folder-relative path (allows one sub-dir, never escapes)."""
        if not isinstance(value, str):
            return ""
        name = value.strip().replace("\\", "/")
        if not name or name.startswith("/") or ".." in name.split("/"):
            return ""
        return name

    def _resolve(self, rel: str) -> Path | None:
        if not rel or not self.source_path:
            return None
        base = Path(self.source_path).parent.resolve()
        p = (base / rel).resolve()
        if base != p and base not in p.parents:  # confine to the card folder
            return None
        return p if p.is_file() else None

    def assets(self) -> dict:
        ext = self.extensions.get("lunamoth")
        a = ext.get("assets") if isinstance(ext, dict) else None
        return a if isinstance(a, dict) else {}

    def asset_path(self, kind: str) -> Path | None:
        """Resolved path of a single-file art asset (sprite/background/keyvisual)."""
        return self._resolve(self._asset_rel(self.assets().get(kind)))

    def sticker_paths(self) -> list[Path]:
        """Resolved, existing sticker sidecar paths (order preserved)."""
        raw = self.assets().get("stickers")
        if not isinstance(raw, list):
            return []
        out = []
        for v in raw:
            p = self._resolve(self._asset_rel(v))
            if p is not None:
                out.append(p)
        return out

    def has_art(self) -> bool:
        """True if any bundled art asset (sprite/background/keyvisual/stickers) exists."""
        return bool(self.asset_path("sprite") or self.asset_path("background")
                    or self.asset_path("keyvisual") or self.sticker_paths())

    def render_system(self, user: str = "User") -> str:
        """Build the persona system block, roughly the way SillyTavern composes it."""
        char = self.name
        parts: list[str] = []
        if self.system_prompt.strip():
            parts.append(apply_macros(self.system_prompt.strip(), char, user))
        header = f"You are {char}. Stay fully in character as {char}. Never break character or reveal you are an AI model."
        parts.append(header)
        if self.description.strip():
            parts.append(f"{char}'s description:\n{apply_macros(self.description.strip(), char, user)}")
        if self.personality.strip():
            parts.append(f"{char}'s personality: {apply_macros(self.personality.strip(), char, user)}")
        if self.scenario.strip():
            parts.append(f"Scenario: {apply_macros(self.scenario.strip(), char, user)}")
        if self.mes_example.strip():
            parts.append(f"Example dialogue:\n{apply_macros(self.mes_example.strip(), char, user)}")
        return "\n\n".join(parts)

    def user_name_override(self) -> str:
        """The operator's name as the card declares it (extensions.lunamoth.user_name),
        or '' — the engine applies it only when the operator hasn't set one."""
        val = self.defaults().get("user_name")
        return str(val).strip() if isinstance(val, str) else ""

    def render_user_persona(self, user: str = "User") -> str:
        """A persona block describing the OPERATOR (the SillyTavern persona-
        description convention), or '' when the card declares none. Lives in
        extensions.lunamoth.user_persona. Stable across a session, so it rides
        the cached prefix — who the user is is not a per-turn fact."""
        val = self.defaults().get("user_persona")
        text = str(val).strip() if isinstance(val, str) else ""
        if not text:
            return ""
        return f"About {user}:\n{apply_macros(text, self.name, user)}"

    def greeting(self, user: str = "User") -> str:
        if not self.first_mes.strip():
            return ""
        return apply_macros(self.first_mes.strip(), self.name, user)


def _entries_as_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = list(raw.values())
    elif isinstance(raw, list):
        items = list(raw)
    else:
        items = []
    return [e for e in items if isinstance(e, dict)]


def _entry_signature(entry: dict[str, Any]) -> tuple[tuple[str, ...], str]:
    keys = entry.get("keys") or entry.get("key") or []
    if isinstance(keys, str):
        keys = [keys]
    return (tuple(str(k) for k in keys), str(entry.get("content", "")))


def merge_world_into_card(card: dict[str, Any], world: dict[str, Any]) -> int:
    """Fold a parsed standalone world book into a card dict's embedded
    `character_book` (the ST import path — the card stays the ONE file).

    Entries are appended in normalized V3 shape; an incoming entry whose
    keys+content match an existing one is skipped. Returns the number of
    entries added. Mutates `card` in place.
    """
    data = card.get("data") if isinstance(card.get("data"), dict) else card
    book = data.get("character_book")
    if not isinstance(book, dict):
        book = {"name": "", "entries": []}
    existing = _entries_as_list(book.get("entries"))
    seen = {_entry_signature(e) for e in existing}
    added = 0
    next_id = len(existing)
    for entry in _entries_as_list(world.get("entries")):
        normalized = entry_to_book_dict(entry, next_id)
        sig = _entry_signature(normalized)
        if sig in seen:
            continue
        existing.append(normalized)
        seen.add(sig)
        next_id += 1
        added += 1
    if not str(book.get("name") or "").strip() and world.get("name"):
        book["name"] = str(world["name"])
    book["entries"] = existing
    data["character_book"] = book
    return added


def looks_like_world_book(obj: Any) -> bool:
    """A standalone ST world book: has `entries`, is not a V2/V3 card."""
    return (
        isinstance(obj, dict)
        and "entries" in obj
        and not isinstance(obj.get("data"), dict)
        and not obj.get("spec")
    )


# ---- card visuals (avatar + art-asset URLs) — ONE source for hub + snapshot -------
# The server's /asset route resolves these p= URLs (confined to card/session
# dirs); the avatar rides an inline data-URI (small) while the heavier art rides
# cacheable same-origin URLs. Shared so the deck list and the live snapshot
# expose the SAME shape from the SAME card path.
_AVATAR_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml"}


def _asset_url(p: "Path | None") -> str:
    if p is None:
        return ""
    import urllib.parse

    return "/asset?p=" + urllib.parse.quote(str(p))


def card_avatar_data_uri(card: "CharacterCard") -> str:
    """A card's avatar as a SMALL inline data-URI: a downscaled WEBP thumbnail of
    the raster sidecar (≤160px, q80) first, inline SVG fallback, else ''.

    This is the INLINE path (board list + StateSnapshot) — it must stay tiny
    (~5–15KB), so a raster sidecar is thumbnailed, not embedded whole. The
    FULL-res sidecar is still reachable via /asset or `card.avatar_read`."""
    sidecar = card.avatar_path()
    if sidecar is not None:
        ext = sidecar.suffix.lower().lstrip(".")
        if ext == "svg":
            # Vector avatar: tiny already, embed as-is (text data-URI).
            try:
                return f"data:image/svg+xml;base64,{base64.b64encode(sidecar.read_bytes()).decode('ascii')}"
            except OSError:
                return ""
        from .imaging import avatar_thumb_data_uri

        thumb = avatar_thumb_data_uri(sidecar)
        if thumb:
            return thumb
        # Thumbnail failed (undecodable): fall back to embedding the raw bytes.
        mime = _AVATAR_MIME.get(ext, "application/octet-stream")
        try:
            data = base64.b64encode(sidecar.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{data}"
        except OSError:
            return ""
    ext = card.extensions.get("lunamoth") if isinstance(card.extensions, dict) else None
    if isinstance(ext, dict):
        svg = ext.get("avatar_svg")
        if isinstance(svg, str) and svg.strip():
            import urllib.parse

            return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg.strip())
    return ""


def card_visuals(card_path: "str | Path") -> dict[str, str]:
    """{avatar_uri, sprite_url, bg_url, keyvisual_url} for one card path.

    All values are strings ('' when absent). Cheap — touches the disk only to
    read the (small) avatar sidecar; the art-asset fields are just URLs the
    server resolves on demand. Used by hub list entries and by the live
    StateSnapshot (from the chara's frozen session card). Never raises: an
    unreadable card yields the empty shape."""
    empty = {"avatar_uri": "", "sprite_url": "", "bg_url": "", "keyvisual_url": ""}
    p = Path(str(card_path or ""))
    if not p.is_file():
        return dict(empty)
    try:
        card = CharacterCard.load(p)
    except Exception:  # noqa: BLE001 — a bad card just has no visuals
        return dict(empty)
    return {
        "avatar_uri": card_avatar_data_uri(card),
        "sprite_url": _asset_url(card.asset_path("sprite")),
        "bg_url": _asset_url(card.asset_path("background")),
        "keyvisual_url": _asset_url(card.asset_path("keyvisual")),
    }
