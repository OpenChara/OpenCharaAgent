"""Desktop hub — roster-level JSON-RPC for the web/desktop renderer.

The hub is the board-level brain of `lunamoth desktop`: it lists charas and
cards, wakes new charas (freezing a card copy), toggles live/idle daemons,
deletes/exports sessions, manages the global model defaults + key testing,
transcribes natural language into card drafts, and reads cross-session files
(works/memory/goals) straight from session directories.

It deliberately NEVER imports core/ or tools/: one process = one activated
session (env-based), so the hub talks to a living chara only through a child
`lunamoth serve <name> --stdio` process (see desktop.py for the proxy). State
the hub reports comes from the documented stable interfaces: session dirs,
`session.json`, `config.json`, the sandbox tree and the transcript SQLite.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..config import ROOT
from ..content.cards import CharacterCard
from ..session import sessions as S
from ..session.settings import PRESETS, Settings
from .dispatch import RpcError, error_response, ok_response, _normalize_request

_log = logging.getLogger("lunamoth.server.hub")

# session isolation level -> python tool execution backend (mirror of front/cli.py)
_ISOLATION_TO_BACKEND = {"dir": "local", "sandbox": "sandbox", "docker": "docker"}

# Models with a reputation for prose ("书写 ★"); heuristic, substring match.
_WRITING_STAR = ("claude", "deepseek-v4", "gpt-5", "gemini-2", "kimi", "grok-4", "qwen3-max")

_HTTP_TIMEOUT = 20.0


# ---- paths -------------------------------------------------------------------

def desktop_config_path() -> Path:
    return S.lunamoth_home() / "desktop.json"


def user_cards_dir() -> Path:
    return S.lunamoth_home() / "cards"


def bundled_cards_dir() -> Path:
    return ROOT / "characters"


# ---- global model defaults -----------------------------------------------------

_DEFAULT_FIELDS = ("provider", "base_url", "api_key", "model", "ui_lang", "ui_theme")


def load_defaults() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        raw = json.loads(desktop_config_path().read_text(encoding="utf-8"))
        for k in _DEFAULT_FIELDS:
            if isinstance(raw.get(k), str):
                data[k] = raw[k]
    except (OSError, json.JSONDecodeError):
        pass
    return data


def save_defaults(updates: dict[str, str]) -> dict[str, str]:
    data = load_defaults()
    for k in _DEFAULT_FIELDS:
        if k in updates and isinstance(updates[k], str):
            data[k] = updates[k]
    path = desktop_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)  # holds an API key
    except OSError:
        pass
    return data


def _public_defaults(data: dict[str, str]) -> dict[str, Any]:
    """Defaults with the key reduced to its presence (never echo secrets)."""
    out: dict[str, Any] = {k: v for k, v in data.items() if k != "api_key"}
    out["has_key"] = bool(data.get("api_key"))
    return out


# ---- provider HTTP (no core/ import; plain OpenAI-compatible calls) ------------

def _http_json(url: str, api_key: str = "", payload: dict | None = None, timeout: float = _HTTP_TIMEOUT) -> Any:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


_models_cache: dict[str, tuple[float, list[dict]]] = {}


def _catalogue(base_url: str, api_key: str = "") -> list[dict]:
    """Provider /models catalogue, cached for the hub's lifetime (10 min TTL)."""
    base = base_url.rstrip("/")
    now = time.monotonic()
    hit = _models_cache.get(base)
    if hit and now - hit[0] < 600:
        return hit[1]
    data = _http_json(base + "/models", api_key)
    models = data.get("data") if isinstance(data, dict) else None
    models = models if isinstance(models, list) else []
    _models_cache[base] = (now, models)
    return models


def model_capabilities(base_url: str, model: str, api_key: str = "") -> dict[str, Any]:
    """Capability badges for one model: tools / vision / writing / context.

    OpenRouter's catalogue is authoritative; other providers report unknown
    (null) rather than guessed values."""
    caps: dict[str, Any] = {"tools": None, "vision": None, "context": None,
                            "writing": any(s in model.lower() for s in _WRITING_STAR)}
    try:
        for m in _catalogue(base_url, api_key):
            if m.get("id") == model:
                params = m.get("supported_parameters") or []
                caps["tools"] = "tools" in params
                arch = m.get("architecture") or {}
                caps["vision"] = "image" in (arch.get("input_modalities") or [])
                caps["context"] = m.get("context_length")
                break
    except Exception:  # noqa: BLE001 - capability probing is best-effort
        _log.debug("capability probe failed", exc_info=True)
    return caps


def test_key(provider: str, base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """One tiny completion: the only honest connectivity test."""
    base = base_url.rstrip("/")
    try:
        data = _http_json(
            base + "/chat/completions", api_key,
            {"model": model, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 4},
            timeout=30.0,
        )
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": _classify_http_error(exc.code, detail)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": {"kind": "network", "detail": str(getattr(exc, "reason", exc))}}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": {"kind": "unknown", "detail": str(exc)}}
    text = ""
    try:
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except (AttributeError, IndexError, TypeError):
        pass
    if not text and isinstance(data, dict) and data.get("error"):
        err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
        return {"ok": False, "error": {"kind": "provider", "detail": str(err.get("message", ""))}}
    return {"ok": True, "model": model, "capabilities": model_capabilities(base, model, api_key)}


def _classify_http_error(code: int, detail: str) -> dict[str, str]:
    """Human-language error classes the UI shows verbatim (design §3.2)."""
    if code in (401, 403):
        return {"kind": "auth", "detail": detail}
    if code == 402 or "credit" in detail.lower() or "balance" in detail.lower():
        return {"kind": "credit", "detail": detail}
    if code == 404:
        return {"kind": "model", "detail": detail}
    if code == 429:
        return {"kind": "ratelimit", "detail": detail}
    return {"kind": "provider", "detail": detail or f"HTTP {code}"}


def _complete(defaults: dict[str, str], system: str, user: str, model: str = "",
              max_tokens: int = 4096, temperature: float = 0.8) -> str:
    base = (defaults.get("base_url") or "").rstrip("/")
    if not base:
        raise RpcError(-32030, "no model configured — set up a provider first")
    data = _http_json(
        base + "/chat/completions", defaults.get("api_key", ""),
        {
            "model": model or defaults.get("model", ""),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": temperature,
        },
        timeout=180.0,
    )
    try:
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except (AttributeError, IndexError, TypeError):
        return ""


# ---- cards ---------------------------------------------------------------------

def _card_sources() -> dict[str, list[str]]:
    """original card path -> session names that froze a copy of it."""
    refs: dict[str, list[str]] = {}
    for meta in S.list_sessions():
        src = meta.root / "card_source"
        try:
            original = src.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if original:
            refs.setdefault(original, []).append(meta.name)
    return refs


def _card_entry(path: Path, builtin: bool, refs: dict[str, list[str]]) -> dict[str, Any] | None:
    try:
        card = CharacterCard.load(path)
    except Exception:  # noqa: BLE001 - one bad card must not break the deck
        _log.warning("unreadable card: %s", path, exc_info=True)
        return None
    ext = card.extensions.get("lunamoth", {}) if isinstance(card.extensions, dict) else {}
    world = ""
    if isinstance(ext, dict):
        world = str(ext.get("world") or "")
    used_by = refs.get(str(path), [])
    return {
        "path": str(path),
        "name": card.name or path.stem,
        "lang": card.language,
        "tags": list(card.tags or [])[:4],
        "world": Path(world).stem if world else "",
        "builtin": builtin,
        "draft": bool(isinstance(ext, dict) and ext.get("draft")),
        "frozen": bool(used_by),
        "used_by": used_by,
        "creator_notes": (card.creator_notes or "")[:300],
    }


def list_cards() -> list[dict[str, Any]]:
    refs = _card_sources()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for base, builtin in ((user_cards_dir(), False), (bundled_cards_dir(), True)):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if p.suffix.lower() not in (".json", ".png") or p.name.startswith("."):
                continue
            if p.stem.startswith("LICENSE"):
                continue
            entry = _card_entry(p, builtin, refs)
            if entry and entry["name"] + entry["lang"] not in seen:
                out.append(entry)
                seen.add(entry["name"] + entry["lang"])
    return out


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str, fallback: str = "chara") -> str:
    s = _SLUG_RE.sub("-", name).strip("-._")
    if not s or not S.valid_name(s):
        s = fallback
    return s[:48]


def save_card(data: dict[str, Any], path: str = "") -> dict[str, Any]:
    """Write a V3 card JSON into the user deck (create flow / drafts)."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise RpcError(-32602, "card.save expects a {spec, data:{...}} card object")
    name = str(data["data"].get("name") or "").strip()
    if not name:
        raise RpcError(-32602, "the card needs a name")
    target: Path
    if path:
        target = Path(path)
        if user_cards_dir() not in target.parents:
            raise RpcError(-32031, "only cards in the user deck can be written")
    else:
        base = user_cards_dir()
        base.mkdir(parents=True, exist_ok=True)
        stem = _slug(name)
        target = base / f"{stem}.json"
        n = 2
        while target.exists():
            target = base / f"{stem}-{n}.json"
            n += 1
    data.setdefault("spec", "chara_card_v3")
    data.setdefault("spec_version", "3.0")
    data["name"] = name
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target)}


def delete_card(path: str) -> dict[str, Any]:
    p = Path(path)
    if user_cards_dir() not in p.parents:
        raise RpcError(-32031, "built-in cards cannot be deleted")
    if _card_sources().get(str(p)):
        raise RpcError(-32032, "this card is referenced by a living chara")
    p.unlink(missing_ok=True)
    return {"ok": True}


# ---- sessions / charas -----------------------------------------------------------

def _read_config(meta: S.SessionMeta) -> dict[str, Any]:
    try:
        return json.loads(meta.config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _transcript_preview(meta: S.SessionMeta) -> dict[str, Any] | None:
    """Last conversational line, read-only, straight from the transcript DB.

    Returns {role, text, ts, awaiting} where awaiting=True means the last chat
    line is the chara's (it spoke and nobody answered — '等你回话')."""
    db = meta.sandbox_dir / "transcript.db"
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT role, content, ts FROM messages "
                "WHERE kind='chat' AND role IN ('user','assistant') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    role, content, ts = row
    text = " ".join(str(content).split())
    return {"role": role, "text": text[:160], "ts": ts, "awaiting": role == "assistant"}


def _last_error(meta: S.SessionMeta) -> str:
    """Most recent line of the chara's error log, if it is fresh (< 10 min)."""
    err = meta.sandbox_dir / "logs" / "errors.log"
    try:
        if time.time() - err.stat().st_mtime > 600:
            return ""
        lines = [ln for ln in err.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return lines[-1][:200] if lines else ""
    except OSError:
        return ""


def session_entry(meta: S.SessionMeta) -> dict[str, Any]:
    cfg = _read_config(meta)
    char_path = (cfg.get("character_path") or "").strip()
    char_name, lang = meta.name, "zh"
    if char_path:
        try:
            card = CharacterCard.load(char_path)
            char_name, lang = card.name or Path(char_path).stem, card.language
        except Exception:  # noqa: BLE001
            char_name = Path(char_path).stem
    return {
        "name": meta.name,
        "char_name": char_name,
        "lang": lang,
        "status": meta.status(),
        "isolation": meta.isolation,
        "model": cfg.get("model", ""),
        "mode": cfg.get("mode", "live"),
        "created_at": meta.created_at,
        "last_active": meta.last_active or meta.created_at,
        "preview": _transcript_preview(meta),
        "error": _last_error(meta),
    }


def wake(card_path: str, name: str = "", isolation: str = "sandbox",
         model: str = "", toolpack: str = "") -> dict[str, Any]:
    """Instantiate a card: create the session, freeze a card copy, write config.

    The card describes WHO the chara is; this call decides where it lives
    (isolation) and what it thinks with (model). The frozen copy means later
    edits to the deck never drift a living chara's persona."""
    card = CharacterCard.load(card_path)  # validates before any disk writes
    defaults = load_defaults()
    if not (defaults.get("base_url") and defaults.get("api_key")) and defaults.get("provider") != "mock":
        raise RpcError(-32030, "no model configured — set up a provider first")
    session_name = _slug(name or Path(card_path).stem)
    base = session_name
    n = 2
    while S.load_session(session_name) is not None:
        session_name = f"{base}-{n}"
        n += 1
    meta = S.create_session(session_name, isolation=isolation if isolation in S.ISOLATION_LEVELS else "sandbox")

    frozen = meta.root / "card.json"
    src = Path(card_path)
    if src.suffix.lower() == ".png":
        # PNG cards keep their embedded payload; copy byte-for-byte.
        frozen = meta.root / "card.png"
        shutil.copyfile(src, frozen)
    else:
        shutil.copyfile(src, frozen)
    (meta.root / "card_source").write_text(str(src), encoding="utf-8")

    card_defaults = card.defaults() if hasattr(card, "defaults") else {}
    cfg = dataclasses.asdict(Settings())
    cfg.update({
        "provider": defaults.get("provider", "openrouter"),
        "base_url": defaults.get("base_url", ""),
        "api_key": defaults.get("api_key", ""),
        "model": model or defaults.get("model", cfg["model"]),
        "character_path": str(frozen),
        "py_backend": _ISOLATION_TO_BACKEND.get(meta.isolation, "sandbox"),
    })
    if toolpack:
        cfg["toolpack"] = toolpack
    elif isinstance(card_defaults, dict) and card_defaults.get("toolpack"):
        cfg["toolpack"] = str(card_defaults["toolpack"])
    meta.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        meta.config_path.chmod(0o600)
    except OSError:
        pass
    return session_entry(meta)


def start_daemon(meta: S.SessionMeta, patience: float = 2.0) -> bool:
    """Spawn the detached background life (mirror of front/cli._start_daemon)."""
    if meta.daemon_pid():
        return True
    if not meta.is_configured():
        return False
    env = {**os.environ, **meta.env()}
    env.setdefault("LUNAMOTH_PY_BACKEND", _ISOLATION_TO_BACKEND[meta.isolation])
    log = meta.daemon_log.open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "lunamoth.front.terminal", "--patience", str(patience)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, env=env, cwd=str(ROOT),
    )
    meta.daemon_pid_path.write_text(str(proc.pid), encoding="utf-8")
    meta.last_active = time.time()
    meta.save()
    return True


def stop_daemon(meta: S.SessionMeta) -> bool:
    import signal

    pid = meta.daemon_pid()
    if not pid:
        meta.daemon_pid_path.unlink(missing_ok=True)
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    meta.daemon_pid_path.unlink(missing_ok=True)
    return True


def export_session(meta: S.SessionMeta) -> dict[str, Any]:
    """Zip the whole session dir (sandbox + transcript + memory + config)."""
    downloads = Path.home() / "Downloads"
    target_dir = downloads if downloads.is_dir() else Path.home()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"lunamoth-{meta.name}-{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(meta.root.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(meta.root.parent))
    return {"path": str(target)}


# ---- sandbox reads for the drawer ------------------------------------------------

_WORK_SKIP_DIRS = {"logs", "memory", "__pycache__", ".git", "node_modules"}
_KIND_BY_EXT = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image", ".svg": "image",
    ".html": "web", ".htm": "web",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".flac": "audio", ".mid": "audio",
    ".md": "text", ".txt": "text",
    ".py": "code", ".js": "code", ".ts": "code", ".sh": "code", ".json": "code", ".css": "code",
}


def list_works(meta: S.SessionMeta, limit: int = 200) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for base in (meta.sandbox_dir / "workspace", meta.sandbox_dir / "files"):
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.name.startswith("."):
                continue
            if any(part in _WORK_SKIP_DIRS or part.startswith(".") for part in p.parts):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": p.name,
                "rel": str(p.relative_to(meta.sandbox_dir)),
                "path": str(p),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "kind": _KIND_BY_EXT.get(p.suffix.lower(), "file"),
            })
    out.sort(key=lambda w: w["mtime"], reverse=True)
    return out[:limit]


def _read_optional(path: Path, limit: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except OSError:
        return ""


def chara_extras(meta: S.SessionMeta) -> dict[str, Any]:
    """Drawer data the hub can read without a living process."""
    sandbox = meta.sandbox_dir
    goals: Any = None
    raw_goals = _read_optional(sandbox / "goals.json")
    if raw_goals:
        try:
            goals = json.loads(raw_goals)
        except json.JSONDecodeError:
            goals = None
    return {
        "memory": _read_optional(sandbox / "memory" / "memory.md"),
        "user_memory": _read_optional(sandbox / "memory" / "user.md"),
        "goals": goals,
        "sandbox_root": str(sandbox),
        "workspace_root": str(sandbox / "workspace"),
    }


def open_path(path: str, reveal: bool = False) -> dict[str, Any]:
    """Hand a file to the OS (design: we present existence, the system opens it)."""
    p = Path(path)
    home = S.lunamoth_home()
    if not p.exists():
        raise RpcError(-32040, "file not found")
    allowed = home in p.parents or p == home or (Path.home() / "Downloads") in p.parents
    if not allowed:
        raise RpcError(-32041, "path is outside the LunaMoth home")
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(p)] if reveal else ["open", str(p)]
    else:
        cmd = ["xdg-open", str(p.parent if reveal else p)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


# ---- natural language -> card draft ----------------------------------------------

_TRANSCRIBE_SYSTEM = """You turn a person's free-form description of an original character (OC) \
into a structured character card. Write in the SAME LANGUAGE as the user's text. Preserve their \
ideas and wording where possible — you are a careful editor, not a co-author. Fill gaps \
conservatively and tastefully; never invent contradictions. Reply with ONLY a JSON object, \
no markdown fence, with exactly these keys:
{"name": str, "appearance": str, "personality": str, "scenario": str, "first_mes": str,
 "alternate_greetings": [str], "world": [{"key": str, "desc": str, "constant": bool}],
 "relationship": str, "goals": [str], "rules": str, "toolpack_hint": str}
- appearance: who they are + how they look, 2-4 sentences, prose.
- personality: temperament and voice, 2-4 sentences, prose.
- first_mes: their in-character opening line when meeting the user.
- world: 2-5 lorebook entries (key = a name/term, desc = one sentence); constant=true for at most one core entry.
- relationship: the user's place in this character's life, 1-2 sentences.
- goals: 1-3 ongoing pursuits, short phrases.
- rules: boundaries/never-dos if implied, else "".
- toolpack_hint: "sandbox" if this character would plausibly make things (art/code/writing), else ""."""


def transcribe_card(defaults: dict[str, str], text: str, model: str = "") -> dict[str, Any]:
    raw = _complete(defaults, _TRANSCRIBE_SYSTEM, text.strip(), model=model)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        draft = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RpcError(-32050, f"the model did not return a usable draft ({exc})") from exc
    if not isinstance(draft, dict) or not draft.get("name"):
        raise RpcError(-32050, "the model did not return a usable draft")
    return draft


def draft_to_card(draft: dict[str, Any], origin_text: str = "", as_draft: bool = False) -> dict[str, Any]:
    """Assemble a V3 card object from a (possibly user-edited) draft."""
    world_entries = []
    for i, w in enumerate(draft.get("world") or []):
        if not isinstance(w, dict) or not w.get("key"):
            continue
        world_entries.append({
            "id": i,
            "keys": [str(w["key"])],
            "content": str(w.get("desc", "")),
            "constant": bool(w.get("constant")),
            "enabled": True,
            "insertion_order": i,
        })
    ext: dict[str, Any] = {"origin": origin_text[:8000]}
    if as_draft:
        ext["draft"] = True
    if draft.get("goals"):
        ext["goals"] = [str(g) for g in draft["goals"]][:5]
    if draft.get("rules"):
        ext["rules"] = str(draft["rules"])
    if draft.get("toolpack_hint"):
        ext["toolpack"] = str(draft["toolpack_hint"])
    data: dict[str, Any] = {
        "name": str(draft.get("name", "")),
        "description": str(draft.get("appearance", "")),
        "personality": str(draft.get("personality", "")),
        "scenario": str(draft.get("scenario", "")) + (
            ("\n\n" + str(draft["relationship"])) if draft.get("relationship") else ""),
        "first_mes": str(draft.get("first_mes", "")),
        "mes_example": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [str(g) for g in (draft.get("alternate_greetings") or [])][:4],
        "creator_notes": "",
        "tags": ["original"],
        "extensions": {"lunamoth": ext},
    }
    if world_entries:
        data["character_book"] = {"name": f"{data['name']} world", "entries": world_entries}
    return {"spec": "chara_card_v3", "spec_version": "3.0", "name": data["name"], "data": data}


# ---- the dispatcher ----------------------------------------------------------------

class HubDispatcher:
    """Board-level JSON-RPC. All handlers are synchronous and run off the event
    loop (the transport calls dispatch() in a worker thread)."""

    def __init__(self, write: Callable[[dict[str, Any]], object]):
        self._write = write

    def dispatch(self, req: Any) -> dict[str, Any] | None:
        normalized = _normalize_request(req)
        if isinstance(normalized, dict):
            return normalized
        rid, method, params, wants_response = normalized
        try:
            result = self._handle(method, params)
        except RpcError as exc:
            return error_response(rid, exc.code, exc.message) if wants_response else None
        except Exception as exc:  # noqa: BLE001 - JSON-RPC is the public error boundary
            _log.exception("hub handler failed method=%s", method)
            return error_response(rid, -32000, f"handler error: {exc}") if wants_response else None
        return ok_response(rid, result) if wants_response else None

    # -- handlers ---------------------------------------------------------------

    def _handle(self, method: str, p: dict[str, Any]) -> Any:
        if method == "hub.state":
            defaults = load_defaults()
            sessions = [session_entry(m) for m in S.list_sessions()]
            return {
                "version": __version__,
                "first_run": not desktop_config_path().exists() and not sessions,
                "defaults": _public_defaults(defaults),
                "presets": {k: {kk: vv for kk, vv in v.items() if kk != "api_key"} for k, v in PRESETS.items()},
                "sessions": sessions,
                "cards": list_cards(),
                "home": str(S.lunamoth_home()),
            }
        if method == "sessions.list":
            return [session_entry(m) for m in S.list_sessions()]
        if method == "session.start":
            meta = self._meta(p)
            if not start_daemon(meta):
                raise RpcError(-32033, "chara is not set up yet")
            return session_entry(meta)
        if method == "session.stop":
            meta = self._meta(p)
            stop_daemon(meta)
            return session_entry(meta)
        if method == "session.delete":
            meta = self._meta(p)
            if p.get("confirm") != meta.name:
                raise RpcError(-32034, "confirmation text does not match")
            stop_daemon(meta)
            S.delete_session(meta.name)
            return {"ok": True}
        if method == "session.export":
            return export_session(self._meta(p))
        if method == "session.wake":
            return wake(
                card_path=str(p.get("card") or ""),
                name=str(p.get("name") or ""),
                isolation=str(p.get("isolation") or "sandbox"),
                model=str(p.get("model") or ""),
                toolpack=str(p.get("toolpack") or ""),
            )
        if method == "chara.extras":
            return chara_extras(self._meta(p))
        if method == "works.list":
            return list_works(self._meta(p))
        if method == "works.open":
            return open_path(str(p.get("path") or ""), reveal=bool(p.get("reveal")))
        if method == "cards.list":
            return list_cards()
        if method == "card.read":
            path = Path(str(p.get("path") or ""))
            try:
                card = CharacterCard.load(path)
            except Exception as exc:  # noqa: BLE001
                raise RpcError(-32035, f"unreadable card: {exc}") from exc
            raw: Any = None
            if path.suffix.lower() == ".json":
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raw = None
            return {"name": card.name, "description": card.description,
                    "personality": card.personality, "scenario": card.scenario,
                    "first_mes": card.first_mes, "alternate_greetings": card.alternate_greetings,
                    "creator_notes": card.creator_notes, "tags": card.tags,
                    "language": card.language, "raw": raw}
        if method == "card.save":
            return save_card(p.get("data"), path=str(p.get("path") or ""))
        if method == "card.delete":
            return delete_card(str(p.get("path") or ""))
        if method == "card.from_draft":
            draft = p.get("draft")
            if not isinstance(draft, dict):
                raise RpcError(-32602, "card.from_draft expects a draft object")
            return save_card(
                draft_to_card(draft, origin_text=str(p.get("origin") or ""), as_draft=bool(p.get("as_draft"))),
                path=str(p.get("path") or ""),
            )
        if method == "defaults.get":
            return _public_defaults(load_defaults())
        if method == "defaults.set":
            updates = {k: v for k, v in p.items() if k in _DEFAULT_FIELDS and isinstance(v, str)}
            return _public_defaults(save_defaults(updates))
        if method == "key.test":
            defaults = load_defaults()
            return test_key(
                provider=str(p.get("provider") or defaults.get("provider", "")),
                base_url=str(p.get("base_url") or defaults.get("base_url", "")),
                api_key=str(p.get("api_key") or defaults.get("api_key", "")),
                model=str(p.get("model") or defaults.get("model", "")),
            )
        if method == "models.list":
            defaults = load_defaults()
            base = str(p.get("base_url") or defaults.get("base_url", ""))
            key = str(p.get("api_key") or defaults.get("api_key", ""))
            try:
                models = _catalogue(base, key)
            except Exception as exc:  # noqa: BLE001
                raise RpcError(-32036, f"could not list models: {exc}") from exc
            out = []
            for m in models:
                params = m.get("supported_parameters") or []
                arch = m.get("architecture") or {}
                out.append({
                    "id": m.get("id"), "name": m.get("name") or m.get("id"),
                    "context": m.get("context_length"),
                    "tools": ("tools" in params) if params else None,
                    "vision": "image" in (arch.get("input_modalities") or []),
                    "writing": any(s in str(m.get("id", "")).lower() for s in _WRITING_STAR),
                })
            return out
        if method == "transcribe.card":
            text = str(p.get("text") or "").strip()
            if not text:
                raise RpcError(-32602, "transcribe.card needs text")
            return transcribe_card(load_defaults(), text, model=str(p.get("model") or ""))
        if method == "open.path":
            return open_path(str(p.get("path") or ""), reveal=bool(p.get("reveal")))
        raise RpcError(-32601, f"unknown method: {method}")

    @staticmethod
    def _meta(p: dict[str, Any]) -> S.SessionMeta:
        name = str(p.get("name") or "")
        meta = S.load_session(name)
        if meta is None:
            raise RpcError(-32004, f"no chara named {name!r}")
        return meta
