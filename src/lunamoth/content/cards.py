from __future__ import annotations

import base64
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .worldinfo import Lorebook, apply_macros

# CJK Unified Ideographs (U+4E00–U+9FFF) — i.e. "does this text contain Han characters".
_CJK = re.compile(r"[\u4e00-\u9fff]")


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
        """The card's recommended world / tool pack / limits.

        Lives in `extensions.lunamoth` (SillyTavern-compatible free-form field):
            {"world": "worlds/X.json", "toolpack": "sandbox", "memory_chars": 8000,
             "goals": ["..."]}
        The context window is NOT here — it's the model's real window (providers.py).
        Cards that omit this block (e.g. plain SillyTavern imports) just get the
        global fallbacks — so any imported card Just Works.
        """
        ext = self.extensions.get("lunamoth")
        if not isinstance(ext, dict):
            return {}
        out = dict(ext)
        goals = out.get("goals")
        if isinstance(goals, list):
            out["goals"] = [str(g).strip() for g in goals if str(g).strip()]
        else:
            out.pop("goals", None)
        return out

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

    def greeting(self, user: str = "User") -> str:
        if not self.first_mes.strip():
            return ""
        return apply_macros(self.first_mes.strip(), self.name, user)
