"""Session/chara lifecycle: roster entries, transcript reads, superchat, the
messaging-gateway config, personal-WeChat QR login, wake/set_modules, the daemon
start/stop, works browsing, and the session export bundle.
"""
from __future__ import annotations

import base64
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
import zipfile
from pathlib import Path
from typing import Any

from ...config import ROOT, content_dir
from ...content.cards import CharacterCard
from ...content.knobs import normalize_embodiment, normalize_website
from ...session import sessions as S
from ...session.settings import Settings
from ..dispatch import RpcError
from ._common import _atomic_write_json, _slug, card_write_lock
from .cards import _copy_card_assets, _merge_preserving, _sanitize_card_extensions
from .config import load_defaults

# messaging gateway config + WeChat QR login live in session_messaging;
# re-exported here so dispatch.py / hub.__init__ keep using _sessions.* unchanged.
from .session_messaging import (  # noqa: F401
    _SECRET_MASK,
    _gateway_status_from_disk,
    _mask_secrets,
    _merge_messaging,
    _read_messaging,
    _unmask_secrets,
    _weixin_config,
    ensure_weixin_adapter,
    messaging_get,
    messaging_save,
    weixin_qr,
    weixin_qr_status,
)

_log = logging.getLogger("lunamoth.server.hub")


def _pkg():
    from .. import hub
    return hub


def _read_config(meta: S.SessionMeta) -> dict[str, Any]:
    try:
        return json.loads(meta.config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _speak_texts_from_struct(content: str) -> list[str]:
    try:
        msg = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(msg, dict):
        return []
    out: list[str] = []
    calls = msg.get("tool_calls")
    if not isinstance(calls, list):
        return out
    for tc in calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict) or fn.get("name") != "speak":
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                continue
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            continue
        if not isinstance(args, dict):
            continue
        raw_text = args.get("text")
        if not isinstance(raw_text, str):
            continue
        text = raw_text.strip()
        if text:
            out.append(" ".join(text.split())[:240])
    return out


def _transcript_speaks(meta: S.SessionMeta, limit: int = 3) -> list[dict[str, Any]]:
    """Newest speak-tool utterances for the board Super Chat feed."""
    db = meta.sandbox_dir / "transcript.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT content, ts FROM messages "
                "WHERE kind='struct' AND role='assistant' AND content LIKE '%speak%' "
                "ORDER BY id DESC LIMIT 80"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for content, ts in rows:
        for text in reversed(_speak_texts_from_struct(str(content))):
            out.append({"text": text, "ts": float(ts or 0.0)})
            if len(out) >= limit:
                return out
    return out


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


_HTTP_CODE_RE = re.compile(r"\bHTTP\s+(401|402|403|404|408|429|500|502|503|504|520|522|524)\b", re.IGNORECASE)


def board_error_kind(line: str) -> str:
    """Classify a recent errors.log line for the desktop board chip."""
    low = line.lower()
    m = _HTTP_CODE_RE.search(line)
    code = int(m.group(1)) if m else 0
    if code in (401, 403) or "auth" in low or "invalid key" in low or "unauthorized" in low:
        return "auth"
    if code == 402 or "credit" in low or "balance" in low:
        return "credit"
    if code == 404 or "model not found" in low:
        return "model"
    if code == 429 or "rate limit" in low or "ratelimit" in low:
        return "ratelimit"
    if any(s in low for s in ("timeout", "connect", "network", "unreachable", "connection failed")):
        return "network"
    return "provider" if line else ""


def _superchat_path(meta: S.SessionMeta) -> Path:
    return meta.root / "superchat.json"


def superchat_read_ts(meta: S.SessionMeta) -> float:
    try:
        data = json.loads(_superchat_path(meta).read_text(encoding="utf-8"))
        return float(data.get("read_ts") or 0.0) if isinstance(data, dict) else 0.0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def set_superchat_read(meta: S.SessionMeta, ts: float) -> dict[str, Any]:
    cur = superchat_read_ts(meta)
    want = max(cur, float(ts or 0.0))
    path = _superchat_path(meta)
    _atomic_write_json(path, {"read_ts": want}, private=True)
    return {"read_ts": want, "superchat_unread": superchat_unread(meta)}


def superchat_unread(meta: S.SessionMeta) -> int:
    read_ts = superchat_read_ts(meta)
    return sum(1 for sp in _transcript_speaks(meta, limit=1000) if float(sp.get("ts") or 0.0) > read_ts)





def session_entry(meta: S.SessionMeta, supervisor: Any | None = None) -> dict[str, Any]:
    cfg = _read_config(meta)
    last_error = _last_error(meta)
    char_path = (cfg.get("character_path") or "").strip()
    char_name, lang = meta.name, "zh"
    if char_path:
        try:
            card = CharacterCard.load(char_path)
            char_name, lang = card.name or Path(char_path).stem, card.language
        except Exception:  # noqa: BLE001
            char_name = Path(char_path).stem
    life = supervisor.life_state(meta.name) if supervisor is not None else None
    gateway = supervisor.gateway_status(meta.name) if supervisor is not None else _gateway_status_from_disk(meta)
    child_status = supervisor.chara_status(meta.name) if supervisor is not None else None
    status = meta.status()
    error = last_error
    if isinstance(child_status, dict) and child_status.get("state") == "crashed":
        status = "crashed"
        error = str(child_status.get("detail") or "crashed")
    # Autonomy is the chara's persisted `mode` (live = autonomous, chat = plain
    # chat agent) — the ONE switch the board and the in-chat panel both flip.
    # `paused` = autonomy off; the board shows it even while the child is up.
    paused = str(cfg.get("mode") or "live") != "live"
    if paused and status != "crashed":
        status = "paused"
    return {
        "name": meta.name,
        "char_name": char_name,
        "lang": lang,
        "status": status,
        "paused": paused,
        "chara": child_status,
        "isolation": meta.isolation,
        "model": cfg.get("model", ""),
        "mode": cfg.get("mode", "live"),
        # the chara's card changed since its process started → UI shows 待应用 +「立即应用」
        "card_dirty": bool(cfg.get("card_dirty")),
        "created_at": meta.created_at,
        "last_active": meta.last_active or meta.created_at,
        "speaks": _transcript_speaks(meta),
        "life": life,
        "gateway": gateway,
        "superchat_unread": superchat_unread(meta),
        "error": error,
        "error_kind": board_error_kind(error),
    }


_HOME_SCAFFOLD = (
    "<!DOCTYPE html>\n"
    "<html lang=\"en\">\n"
    "<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title></title>\n"
    "</head>\n"
    "<body>\n"
    "<!-- This is your homepage. It's yours to shape — replace this freely. -->\n"
    "</body>\n"
    "</html>\n"
)


def _write_home_scaffold(meta: "S.SessionMeta") -> None:
    """Lay down a neutral, character-free home/index.html so the website tab has
    something to render from the start. Never overwrites an existing homepage."""
    try:
        home = meta.sandbox_dir / "workspace" / "home"
        index = home / "index.html"
        if index.exists():
            return
        home.mkdir(parents=True, exist_ok=True)
        index.write_text(_HOME_SCAFFOLD, encoding="utf-8")
    except OSError:
        pass  # best-effort: a missing scaffold just means an empty website tab


def wake(card_path: str, name: str = "", isolation: str = "sandbox",
         model: str = "", toolpack: str = "", embodiment: str = "",
         website: str = "", key: str = "", mode: str = "live",
         network: bool = True,
         card_data: "dict[str, Any] | None" = None) -> dict[str, Any]:
    """Instantiate a card: create the session, freeze a card copy, write config.

    The card describes WHO the chara is; this call decides where it lives
    (isolation), what it thinks with (model) and — once, at wake — how tools
    relate to its fiction (embodiment). Embodiment is a wake-time choice, never
    hot-swapped: identity-layer switches would rebuild the stable prefix and
    destroy the prompt cache. The frozen copy means later edits to the deck
    never drift a living chara's persona."""
    stance = ""
    if embodiment:
        stance = normalize_embodiment(embodiment)
        if not stance:
            raise RpcError(-32602, f"invalid embodiment {embodiment!r} — expected literal|actor")
    web = ""
    if website:
        web = normalize_website(website)
        if not web:
            raise RpcError(-32602, f"invalid website {website!r} — expected on|off")
    card = CharacterCard.load(card_path)  # validates before any disk writes
    defaults = load_defaults()
    if key:
        # A named key (webui-needs #10): its provider/base_url/api_key drive
        # this chara; its model fills in only when wake didn't pick one.
        defaults = {**defaults, **_pkg()._key_overrides(key)}  # wake's `model` param still wins below
    # The key rides the keyring (resolved by route) unless a named-key override set it.
    from ...session.settings import global_api_key
    eff_key = defaults.get("api_key") or global_api_key(str(defaults.get("provider") or ""), str(defaults.get("base_url") or ""))
    if not (defaults.get("base_url") and eff_key) and defaults.get("provider") != "mock":
        raise RpcError(-32030, "no model configured — set up a provider first")
    session_name = _slug(name or Path(card_path).stem)
    base = session_name
    n = 2
    while S.load_session(session_name) is not None:
        session_name = f"{base}-{n}"
        n += 1
    iso = S.normalize_isolation(isolation)  # legacy dir/local/docker → admin
    from ...session.isolation import force_sandbox
    if force_sandbox():
        iso = "sandbox"  # distribution lock: wake every chara jailed regardless of the request
    meta = S.create_session(session_name, isolation=iso if iso in S.ISOLATION_LEVELS else "sandbox")

    frozen = meta.root / "card.json"
    src = Path(card_path)
    if card_data is not None:
        # Wake-time edits: freeze the EDITED card as this chara's own card; the
        # source template is never mutated (it stays unlocked and re-wakeable).
        if not isinstance(card_data, dict) or not isinstance(card_data.get("data"), dict):
            raise RpcError(-32602, "card_data must be a {data:{...}} card object")
        edited = dict(card_data)
        edited.setdefault("version", "1.0")  # our own card format (no ST spec markers)
        # ROOT FIX (data-loss): merge the edit ONTO the freshly-loaded SOURCE card
        # so a blank/partial submission from the wake editor can never freeze a
        # persona-less, greeting-less chara. The editor renders no field for
        # mes_example / system_prompt / post_history_instructions, and a card.read
        # hiccup blanks the rest — without this merge those overwrite the source
        # with "". An empty edited field keeps the source value; a real edit wins.
        from ...content.cards import _card_json_from_png as _png_json
        try:
            src_dict = _png_json(src) if src.suffix.lower() == ".png" else json.loads(src.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — unreadable source → freeze the edit as-is
            src_dict = {}
        if isinstance(src_dict, dict) and isinstance(src_dict.get("data"), dict):
            edited["data"] = _merge_preserving(src_dict["data"], edited.get("data") or {})
            if not str(edited.get("name") or "").strip() and src_dict.get("name"):
                edited["name"] = src_dict["name"]
        _sanitize_card_extensions(edited)
        frozen.write_text(json.dumps(edited, ensure_ascii=False, indent=2), encoding="utf-8")
    elif src.suffix.lower() == ".png":
        # PNG cards keep their embedded payload; copy byte-for-byte.
        frozen = meta.root / "card.png"
        shutil.copyfile(src, frozen)
    else:
        shutil.copyfile(src, frozen)
    (meta.root / "card_source").write_text(str(src), encoding="utf-8")
    # Freeze a copy of the art-asset library beside the frozen card, so a living
    # chara owns its own visuals (the deck/chat resolve them from the session dir,
    # not the deck template that may later change or be deleted). Use the FROZEN
    # card's declarations (it may have been edited) but read the files from the
    # source template folder where they actually live.
    asset_decl = card
    if card_data is not None and src.suffix.lower() != ".png":
        try:
            asset_decl = CharacterCard.load(frozen)
        except Exception:  # noqa: BLE001 - fall back to the template's declarations
            asset_decl = card
    _copy_card_assets(asset_decl, meta.root, src_base=Path(card_path).parent)

    card_defaults = card.defaults() if hasattr(card, "defaults") else {}
    cfg = dataclasses.asdict(Settings())
    cfg.update({
        "provider": defaults.get("provider", "openrouter"),
        "base_url": defaults.get("base_url", ""),
        # SEC-2: do NOT copy the api_key into the session config — it's resolved at
        # load from the global keyring (settings.global_api_key). Sessions hold only
        # non-secret overrides, so the key isn't duplicated into every chara's dir.
        "model": model or defaults.get("model", cfg["model"]),
        "model_context": int(defaults.get("model_context") or 0),
        # per-session model knobs the agent/llm read (Settings.reasoning) must be
        # copied from the global defaults at wake, else a woken chara silently
        # ignores the Model-pane choice. (Read-image is GLOBAL — global_vision_route
        # — so vision_model is NOT copied per-chara any more.)
        "reasoning": str(defaults.get("reasoning") or cfg["reasoning"]),
        "character_path": str(frozen),
        # config.json mirrors the AUTHORITY field name (session.json `isolation`),
        # never the derived py_backend — one field, read by downgrade_admin_sessions.
        "isolation": meta.isolation,
    })
    cfg.pop("api_key", None)
    if toolpack:
        cfg["toolpack"] = toolpack
    elif isinstance(card_defaults, dict) and card_defaults.get("toolpack"):
        cfg["toolpack"] = str(card_defaults["toolpack"])
    if stance:
        # Operator's wake-time choice persists as the override; absent, the
        # resolution chain stays card declaration > literal.
        cfg["embodiment_override"] = stance
    if web:
        # personal_website module wake-time choice; absent → card declaration > off.
        cfg["website_override"] = web
    # Autonomy (mode) is the ONE on/off switch the board + in-chat panel share.
    # Wake defaults it live (autonomous); the operator can wake straight into chat.
    cfg["mode"] = "chat" if str(mode).lower() == "chat" else "live"
    # Always lay down a neutral homepage scaffold so the website tab has something
    # to show from the start (the module toggle only controls the prompt guidance).
    _write_home_scaffold(meta)
    meta.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        meta.config_path.chmod(0o600)
    except OSError:
        pass
    # Network is ON by default at runtime (env_status.json); only when the operator
    # wakes with network OFF do we pre-seed the env-state file so the very first run
    # starts cut off. (Written as a literal here — server/ must not import core/.)
    if not network:
        env_path = meta.sandbox_dir / "env_status.json"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(json.dumps({
            "isolation": meta.isolation if meta.isolation in ("sandbox", "admin") else "sandbox",
            "network_access": False,
            "writable_paths": [],
            "rest_until": 0.0,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return session_entry(meta)


def set_modules(meta: S.SessionMeta, force_roleplay: Any = None,
                website: Any = None) -> dict[str, Any]:
    """Toggle a chara's optional prompt modules AFTER wake. Like a memory edit,
    the change is written to the session config and takes effect on the NEXT start
    (never hot-swapped — a module rides the cache-stable prefix; a live rebuild
    would throw away the prompt cache). Pass only the modules you want to change.

    force_roleplay → embodiment_override ('actor' when on, 'literal' when off).
    website        → website_override ('on'/'off'). Either side may be None (leave).
    """
    try:
        cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # A corrupt/unreadable config must NOT be silently reset to fresh defaults
        # (that would wipe the chara's model/isolation/etc). Surface it instead.
        raise RpcError(-32031, f"cannot read session config for {meta.name!r}: {e}") from e
    if force_roleplay is not None:
        cfg["embodiment_override"] = "actor" if bool(force_roleplay) else "literal"
    if website is not None:
        cfg["website_override"] = "on" if bool(website) else "off"
        if bool(website):
            _write_home_scaffold(meta)  # ensure the homepage exists when turned on
    _atomic_write_json(meta.config_path, cfg, private=True)
    return {
        "ok": True,
        "force_roleplay": cfg.get("embodiment_override") == "actor",
        "website": cfg.get("website_override") == "on",
        "applies": "next_start",
    }


def set_aspiration(meta: S.SessionMeta, text: str) -> dict[str, Any]:
    """Set the chara's aspiration (理想 — the user-owned north-star). Writes the LIVE
    ``sandbox/polaris.json`` (a running chara re-reads it EVERY turn → takes effect
    next turn, no restart) AND the frozen session card field so it survives a restart
    or re-wake. The aspiration is read-only to the chara; only the user sets it."""
    text = str(text or "").strip()[:1000]
    # live store — same on-disk format as tools.polaris.PolarisStore (kept inline so
    # the server layer never imports core/tools). Next turn the running chara reads it.
    pol = meta.sandbox_dir / "polaris.json"
    try:
        pol.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(pol, {"polaris": text})
    except OSError as e:
        raise RpcError(-32031, f"could not write the aspiration: {e}") from e
    # persist on the frozen card too (so a restart / re-wake keeps it). Share the card
    # lock so a concurrent card.patch / visual save on the same session card can't lose it.
    card = meta.root / "card.json"
    if card.is_file():
        try:
            with card_write_lock(card):
                raw = json.loads(card.read_text(encoding="utf-8"))
                data = raw.get("data") if isinstance(raw, dict) else None
                if isinstance(data, dict):
                    ext = data.setdefault("extensions", {})
                    lm = ext.setdefault("lunamoth", {}) if isinstance(ext, dict) else None
                    if isinstance(lm, dict):
                        if text:
                            lm["polaris"] = text
                        else:
                            lm.pop("polaris", None)
                        _atomic_write_json(card, raw)
        except (OSError, json.JSONDecodeError):
            pass  # best-effort card persist; the live store is this session's source of truth
    return {"ok": True, "polaris": text, "applies": "next_turn"}


def session_for_card(path: str) -> S.SessionMeta | None:
    """The session that OWNS a frozen card path (``<sessions>/<name>/card.json``), or
    None for a deck/template card."""
    try:
        rp = Path(str(path or "")).resolve()
    except OSError:
        return None
    if rp.name == "card.json" and rp.parent.parent == S.sessions_dir().resolve():
        return S.load_session(rp.parent.name)
    return None


def mark_card_dirty(meta: S.SessionMeta) -> None:
    """Flag that the chara's card changed since its process last started, so the UI can
    show '待应用 / apply pending'. Cleared when the child (re)starts (children.start).
    Locked + atomic so a concurrent config write can't tear it / drop the api_key."""
    with card_write_lock(meta.config_path):
        try:
            cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not cfg.get("card_dirty"):
            cfg["card_dirty"] = True
            _atomic_write_json(meta.config_path, cfg, private=True)


def clear_card_dirty(meta: S.SessionMeta) -> None:
    with card_write_lock(meta.config_path):
        try:
            cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if cfg.pop("card_dirty", None) is not None:
            _atomic_write_json(meta.config_path, cfg, private=True)


def set_isolation(meta: S.SessionMeta, isolation: str) -> dict[str, Any]:
    """Switch a chara's OS isolation (sandbox|admin) AFTER wake. Like set_modules,
    this writes the session config and takes effect on the NEXT process start — the
    jail backend (LUNAMOTH_PY_BACKEND) is pinned when the chara's child launches, so
    it is NEVER hot-swapped under a running chara. Switching to ``admin`` removes the
    sandbox entirely (full-machine read/write at the user's privileges) — a
    deliberate, trust-the-card act, gated by a confirm in the UI."""
    iso = S.normalize_isolation(str(isolation or ""))  # legacy dir/local/docker → admin
    if iso not in S.ISOLATION_LEVELS:
        raise RpcError(-32602, f"isolation must be one of {sorted(S.ISOLATION_LEVELS)}")
    from ...session.isolation import force_sandbox
    if force_sandbox() and iso != "sandbox":
        # Distribution lock: this server pins every chara to the sandbox; admin is refused.
        raise RpcError(-32602, "sandbox is enforced on this server (admin isolation is disabled)")
    # SessionMeta.set_isolation is the ONE writer of both stores (session.json authority +
    # config.json mirror) — it can't leave the jail authority stale the way a config-only
    # write did, and downgrade_admin_sessions routes through the same helper.
    meta.set_isolation(iso)
    return {"ok": True, "isolation": iso, "applies": "next_start"}


def start_daemon(meta: S.SessionMeta, patience: float | None = None) -> bool:
    """Spawn the detached background life (mirror of front/cli._start_daemon)."""
    if meta.daemon_pid():
        return True
    if not meta.is_configured():
        return False
    env = {**os.environ, **meta.env()}  # meta.env() carries LUNAMOTH_PY_BACKEND
    log = meta.daemon_log.open("ab")
    argv = [sys.executable, "-m", "lunamoth.front.terminal"]
    if patience is not None:
        argv += ["--patience", str(patience)]
    proc = subprocess.Popen(
        argv,
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


class _TranscriptReadError(Exception):
    """The transcript DB EXISTS but couldn't be read (locked by a running chara,
    corrupt, schema-mismatch). Distinct from 'no transcript yet' (db absent → "")
    so the export surfaces it instead of silently shipping an empty file."""


def _transcript_export_jsonl(meta: S.SessionMeta) -> str:
    """Hermes-style complete conversation export of the CURRENT epoch, read
    straight from the session's transcript DB (read-only — works while the
    chara is stopped). Every row (chat/think/struct/tool/summary) becomes one
    JSON line, oldest first; struct/tool rows expanded back to their full
    message dict. The hub reads the DB directly (never imports core/).

    Returns "" for a never-run chara (no DB). Raises _TranscriptReadError when the
    DB exists but can't be read — a real failure the caller must surface, never a
    silent empty export."""
    db = meta.sandbox_dir / "transcript.db"
    if not db.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as e:
        raise _TranscriptReadError(f"could not open the transcript DB ({e})") from e
    try:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
            epoch = int(row[0]) if row and row[0] else 0
        except (sqlite3.Error, ValueError):
            epoch = 0
        try:
            rows = conn.execute(
                # kind='replay' is compaction's tail re-append — the same rows sit
                # earlier in the epoch; exporting them would duplicate the tail.
                "SELECT id, ts, role, content, kind FROM messages WHERE epoch=? AND kind != 'replay' ORDER BY id",
                (epoch,),
            ).fetchall()
        except sqlite3.Error as e:
            raise _TranscriptReadError(f"could not read transcript rows ({e})") from e
    finally:
        conn.close()
    out_lines: list[str] = []
    for row_id, ts, role, content, kind in rows:
        obj: dict[str, Any] = {"id": int(row_id), "ts": float(ts or 0.0),
                               "role": str(role), "kind": str(kind)}
        if kind in ("struct", "tool"):
            try:
                msg = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                msg = None
            if isinstance(msg, dict):
                for k, v in msg.items():
                    obj.setdefault(k, v)
                obj["role"] = str(msg.get("role") or role)
            else:
                obj["content"] = str(content)
        else:
            obj["content"] = str(content)
        out_lines.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def export_session(meta: S.SessionMeta) -> dict[str, Any]:
    """Zip the whole session dir, AND emit the complete conversation as JSONL.

    The zip stays the raw forensic bundle (sandbox + transcript + memory +
    config). Alongside it, for debugging like hermes's export, we write:
      <name>-conversation.jsonl — every transcript row of the current epoch
        (prompts/tool calls/results/reasoning), oldest first;
      <name>-requests.jsonl — a copy of sandbox/logs/requests.jsonl (the
        faithful per-turn request log) when it exists.
    Both are placed inside the zip AND as standalone files next to it. The zip
    path is still the primary return value."""
    downloads = Path.home() / "Downloads"
    target_dir = downloads if downloads.is_dir() else Path.home()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"lunamoth-{meta.name}-{stamp}.zip"

    # The conversation jsonl is a convenience view; the raw transcript.db rides in
    # the zip regardless. If the DB exists but can't be read, DON'T ship a silently
    # empty file claiming success — write an honest marker and report the error,
    # while still producing the zip (which carries the raw DB for recovery).
    errors: list[str] = []
    try:
        conversation = _transcript_export_jsonl(meta)
    except _TranscriptReadError as e:
        errors.append(str(e))
        conversation = json.dumps(
            {"_export_error": f"the transcript could not be read: {e}. "
             "The raw transcript.db is still included in the zip."},
            ensure_ascii=False,
        ) + "\n"
    conv_path = target_dir / f"lunamoth-{meta.name}-{stamp}-conversation.jsonl"
    conv_path.write_text(conversation, encoding="utf-8")

    requests_src = meta.sandbox_dir / "logs" / "requests.jsonl"
    requests_path: Path | None = None
    requests_text = ""
    if requests_src.exists():
        try:
            requests_text = requests_src.read_text(encoding="utf-8")
        except OSError:
            requests_text = ""
        requests_path = target_dir / f"lunamoth-{meta.name}-{stamp}-requests.jsonl"
        requests_path.write_text(requests_text, encoding="utf-8")

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(meta.root.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(meta.root.parent))
        zf.writestr(f"{meta.name}-conversation.jsonl", conversation)
        if requests_path is not None:
            zf.writestr(f"{meta.name}-requests.jsonl", requests_text)

    result: dict[str, Any] = {"path": str(target), "conversation": str(conv_path)}
    if requests_path is not None:
        result["requests"] = str(requests_path)
    if errors:
        result["errors"] = errors  # the zip shipped, but surface what couldn't be read
    return result


def list_toolpacks() -> list[dict[str, Any]]:
    """Bundled tool packs for the wake sheet's picker (webui-needs #8/#12).

    Pure data read of toolpacks/*.json — the server never imports tools/."""
    base = content_dir("toolpacks")
    out: list[dict[str, Any]] = []
    if not base.is_dir():
        return out
    for p in sorted(base.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _log.warning("unreadable toolpack skipped: %s", p)
            continue
        out.append({
            "name": str(d.get("name") or p.stem),
            "description": str(d.get("description") or ""),
            "tools": [str(t) for t in (d.get("tools") or [])],
            "mcp_servers": [str(x) for x in (d.get("mcp_servers") or [])],
            "path": str(p),
        })
    return out


# ---- sandbox reads for the drawer ------------------------------------------------

# assets/ is the card's staged reference art (roleplay visuals), not the chara's
# own work — exclude it from the works listing just like skills/ know-how.
_WORK_SKIP_DIRS = {"logs", "memory", "skills", "assets", "__pycache__", ".git", "node_modules"}
_KIND_BY_EXT = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image", ".svg": "image",
    ".html": "web", ".htm": "web",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".flac": "audio", ".mid": "audio",
    ".md": "text", ".txt": "text",
    ".py": "code", ".js": "code", ".ts": "code", ".sh": "code", ".json": "code", ".css": "code",
}


def list_works(meta: S.SessionMeta, limit: int = 200) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # The Works tab is the chara's shareable SHELF: workspace/works/ ONLY. The
    # rest of the workspace is the chara's private working area and is NOT
    # surfaced here; assets/ is a read-only reference sibling (also not "works").
    # _WORK_SKIP_DIRS still drops stray logs/skills/etc if they appear under works/.
    base = meta.sandbox_dir / "workspace" / "works"
    if base.is_dir():
        for p in base.rglob("*"):
            if not p.is_file() or p.name.startswith("."):
                continue
            # Judge only the path UNDER the works tree: the sandbox itself may
            # live below a dot-dir (~/.lunamoth/...), which must not hide it.
            if any(part in _WORK_SKIP_DIRS or part.startswith(".") for part in p.relative_to(base).parts):
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


_WORK_READ_CAP = 512 * 1024
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
}
_TEXT_READ_EXTS = {
    ".md", ".txt", ".py", ".js", ".ts", ".sh", ".json", ".css", ".html", ".htm",
    ".csv", ".yml", ".yaml", ".toml", ".log",
}


def read_work(meta: S.SessionMeta, rel: str) -> dict[str, Any]:
    """In-app preview of one sandbox work (the deck's works page).

    `rel` comes from works.list and must stay inside the sandbox's workspace/
    tree (or the read-only assets/ sibling, so the same preview can render a
    reference asset) — anything else is refused (no traversal). Over-cap files
    return truncated so the UI can offer works.open instead.
    """
    if not rel:
        raise RpcError(-32602, "works.read needs rel")
    sandbox = meta.sandbox_dir.resolve()
    target = (sandbox / rel).resolve()
    workspace = (sandbox / "workspace").resolve()
    assets = (sandbox / "assets").resolve()
    under_ws = workspace == target or workspace in target.parents
    under_assets = assets == target or assets in target.parents
    if not (under_ws or under_assets):
        raise RpcError(-32031, "works.read only serves files under workspace/ or assets/")
    if not target.is_file():
        raise RpcError(-32035, f"no such work: {rel}")
    size = target.stat().st_size
    suffix = target.suffix.lower()
    if suffix in _IMAGE_MIME:
        if size > _WORK_READ_CAP:
            return {"kind": "image", "size": size, "truncated": True}
        data = base64.b64encode(target.read_bytes()).decode("ascii")
        return {"kind": "image", "size": size, "truncated": False,
                "data_uri": f"data:{_IMAGE_MIME[suffix]};base64,{data}"}
    if suffix in _TEXT_READ_EXTS:
        raw = target.read_bytes()
        return {"kind": "text", "size": size, "truncated": len(raw) > _WORK_READ_CAP,
                "content": raw[:_WORK_READ_CAP].decode("utf-8", errors="replace")}
    return {"kind": "binary", "size": size, "truncated": size > _WORK_READ_CAP}


def _read_optional(path: Path, limit: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except OSError:
        return ""


def chara_extras(meta: S.SessionMeta) -> dict[str, Any]:
    """Drawer data the hub can read without a living process."""
    sandbox = meta.sandbox_dir
    polaris = ""
    raw = _read_optional(sandbox / "polaris.json")
    if raw:
        try:
            data = json.loads(raw)
            polaris = str(data.get("polaris") or "").strip() if isinstance(data, dict) else ""
        except json.JSONDecodeError:
            polaris = ""
    return {
        "memory": _read_optional(sandbox / "memory" / "memory.md"),
        "user_memory": _read_optional(sandbox / "memory" / "user.md"),
        "polaris": polaris,
        "tasks": _read_tasks(sandbox),
        "sandbox_root": str(sandbox),
        "workspace_root": str(sandbox / "workspace"),
    }


def _read_tasks(sandbox: Path) -> dict[str, list]:
    """The chara's tasks (active threads + sealed records) read straight off disk,
    in the same {active, done} shape TaskStore.payload() produces. Read directly
    (no tools import) so the hub stays decoupled from core/tools, exactly as the
    polaris read above does. Read-only display."""
    raw = _read_optional(sandbox / "task.json")
    if not raw:
        return {"active": [], "done": []}
    try:
        data = json.loads(raw)
        items = [t for t in data.get("tasks", []) if isinstance(t, dict)]
    except (json.JSONDecodeError, AttributeError):
        return {"active": [], "done": []}
    active = [t for t in items if t.get("status") != "done"]
    done = sorted((t for t in items if t.get("status") == "done"),
                  key=lambda t: t.get("done_at") or 0, reverse=True)
    return {"active": active, "done": done}


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
