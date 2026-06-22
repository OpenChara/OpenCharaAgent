"""Per-chara aspiration — the chara's single lifelong ideal.

`polaris` is the stable internal CODENAME (this module, the store, `polaris.json`,
the card field `extensions.lunamoth.polaris`, the data key). The USER-FACING term
is 理想 / "Aspiration" — all model-facing and UI text says aspiration, never the
codename.

It is a SINGLE grand ideal the chara lives toward but can never fully reach —
unattainable by design, so it has no "done"/"dropped" status. It is authored by
the USER (the card field, the `/aspiration` command, and the UI) and is READ-ONLY
to the chara: the chara cannot edit or complete it. It is injected into every turn
as a quiet guiding star (see `render_block`). Seeded from the card once; persisted
to `polaris.json` in the session sandbox. Best-effort persistence — a failing disk
must never kill the host loop.

(This replaced the old chara-mutable wish/goal list: a chara no longer adds or
completes goals; its one orienting aim is the user's aspiration, never finished.)
"""
from __future__ import annotations

import json
from pathlib import Path


class PolarisStore:
    """A single user-owned north-star string, read-only to the chara."""

    _MAX = 1000

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> str:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get("polaris") or "").strip()
        except (OSError, json.JSONDecodeError):
            pass
        return ""

    def _save(self, text: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"polaris": text}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def get(self) -> str:
        return self._load()

    def set(self, text: str) -> str:
        """Set the Polaris. USER-only — there is no chara tool that calls this;
        the chara can never change its north-star."""
        text = (text or "").strip()[: self._MAX]
        self._save(text)
        return text

    def seed_once(self, text: str) -> bool:
        """Seed from the card, but only when the store is still empty (so a user
        edit on a live chara is never clobbered by a later reconfigure)."""
        text = (text or "").strip()
        if not text or self._load():
            return False
        self._save(text)
        return True

    def render_block(self) -> str:
        """The read-only north-star block for the system prompt ('' when unset)."""
        text = self._load()
        if not text:
            return ""
        return (
            "Your aspiration — the single grand ideal you live toward. Your creator set "
            "it; it is not yours to change, and by its nature you can never fully reach "
            "or 'complete' it. Let it quietly orient what you choose to do:\n"
            f"  {text}"
        )
