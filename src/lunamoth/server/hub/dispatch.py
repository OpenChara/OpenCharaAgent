"""The board-level JSON-RPC dispatcher.

``HubDispatcher`` maps every method string to a handler via a declarative
``{method: handler}`` table (the same pattern tools/registry.py and
server/dispatch.py use) — no if-ladder. Handlers are bound methods taking the
params dict; aliases (``session.start`` / ``chara.start``) share one handler.

LLM-completion handlers reach ``_complete`` through the hub PACKAGE namespace so
a test patching ``H._complete`` is honored (see ``_pkg``).
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Callable

from ... import __version__
from ...session import isolation as _isolation
from ...session import sessions as S
from ...session.settings import PRESETS
from ..dispatch import RpcError, error_response, ok_response, _normalize_request
from . import avatars as _avatars
from . import card_draft as _card_draft
from . import card_market as _card_market
from . import cards as _cards
from . import config as _config
from . import models as _models
from . import sessions as _sessions
from . import updates as _updates
from ._common import HubRpcError, _await_supervisor, _meta

_log = logging.getLogger("lunamoth.server.hub")


def _pkg():
    from .. import hub
    return hub


def _brief_of(card: dict) -> dict | None:
    """The visual brief persisted on a card (extensions.lunamoth.visual_brief), or None."""
    lm = (((card.get("data") or {}).get("extensions") or {}).get("lunamoth") or {})
    b = lm.get("visual_brief")
    return b if isinstance(b, dict) and b else None


def _norm_grid(v: Any) -> tuple[int, int] | None:
    """Normalize a sticker grid request: an int n→(n,n), or [rows,cols]; 1..3 each.
    Returns None (→ the kind default) on anything else."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and 1 <= v <= 3:
        return (v, v)
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            r, c = int(v[0]), int(v[1])
        except (TypeError, ValueError):
            return None
        if 1 <= r <= 3 and 1 <= c <= 3:
            return (r, c)
    return None


def _keyvisual_data_uri(path: str, card: dict) -> str | None:
    """The card's saved keyvisual as a data-URI — the server-side identity ANCHOR fed
    as a reference into the other kinds so the set stays one character even when the
    client isn't managing it. None if there's no keyvisual yet."""
    lm = (((card.get("data") or {}).get("extensions") or {}).get("lunamoth") or {})
    name = (lm.get("assets") or {}).get("keyvisual")
    if not isinstance(name, str) or not name:
        return None
    try:
        kv = Path(path).with_name(name)
        if not kv.is_file():
            return None
        raw = kv.read_bytes()
    except OSError:
        return None
    mime = ("image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n"
            else "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/webp")
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


class HubDispatcher:
    """Board-level JSON-RPC. All handlers are synchronous and run off the event
    loop (the transport calls dispatch() in a worker thread)."""

    def __init__(self, write: Callable[[dict[str, Any]], object], supervisor: Any | None = None):
        self._write = write
        self.supervisor = supervisor
        self._table: dict[str, Callable[[dict[str, Any]], Any]] = self._build_table()

    def dispatch(self, req: Any) -> dict[str, Any] | None:
        normalized = _normalize_request(req)
        if isinstance(normalized, dict):
            return normalized
        rid, method, params, wants_response = normalized
        try:
            result = self._handle(method, params)
        except HubRpcError as exc:
            if not wants_response:
                return None
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": rid, "error": error}
        except RpcError as exc:
            return error_response(rid, exc.code, exc.message) if wants_response else None
        except Exception as exc:  # noqa: BLE001 - JSON-RPC is the public error boundary
            _log.exception("hub handler failed method=%s", method)
            return error_response(rid, -32000, f"handler error: {exc}") if wants_response else None
        return ok_response(rid, result) if wants_response else None

    # -- dispatch table ---------------------------------------------------------

    def _handle(self, method: str, p: dict[str, Any]) -> Any:
        handler = self._table.get(method)
        if handler is None:
            raise RpcError(-32601, f"unknown method: {method}")
        return handler(p)

    def _build_table(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "hub.state": self._hub_state,
            "sessions.list": self._sessions_list,
            "session.start": self._session_start,
            "chara.start": self._session_start,
            "session.stop": self._session_stop,
            "chara.stop": self._session_stop,
            "chara.set_autonomy": self._set_autonomy,
            "gateway.start": self._gateway_start,
            "gateway.stop": self._gateway_stop,
            "gateway.status": self._gateway_status,
            "gateways.list": self._gateways_list,
            "superchat.read": self._superchat_read,
            "session.delete": self._session_delete,
            "session.export": self._session_export,
            "session.wake": self._session_wake,
            "session.set_modules": self._session_set_modules,
            "chara.set_isolation": self._chara_set_isolation,
            "chara.set_aspiration": lambda p: _sessions.set_aspiration(_meta(p), str(p.get("text") or "")),
            "chara.apply_card": self._chara_apply_card,
            "card.visual_jobs": self._card_visual_jobs,
            "toolpacks.list": lambda p: _sessions.list_toolpacks(),
            "keys.list": lambda p: _config.list_keys(),
            "keys.save": self._keys_save,
            "keys.delete": lambda p: _config.delete_key(str(p.get("label") or "")),
            "defaults.use_key": lambda p: _config.use_key(str(p.get("label") or "")),
            "matte.status": self._matte_status,
            "matte.install_deps": self._matte_install_deps,
            "matte.download": self._matte_download,
            "matte.delete": self._matte_delete,
            "matte.use": self._matte_use,
            "chara.extras": lambda p: _sessions.chara_extras(_meta(p)),
            "works.list": lambda p: _sessions.list_works(_meta(p)),
            "works.read": lambda p: _sessions.read_work(_meta(p), str(p.get("rel") or "")),
            "works.open": lambda p: _sessions.open_path(str(p.get("path") or ""), reveal=bool(p.get("reveal"))),
            "messaging.get": lambda p: _sessions.messaging_get(_meta(p)),
            "messaging.save": lambda p: _sessions.messaging_save(_meta(p), p.get("config")),
            "weixin.qr": lambda p: _sessions.weixin_qr(_meta(p)),
            "weixin.qr_status": lambda p: _sessions.weixin_qr_status(_meta(p), str(p.get("qrcode") or "")),
            "card.avatar_upload": self._card_avatar_upload,
            "card.visual_brief": self._card_visual_brief,
            "card.visual_brief_save": self._card_visual_brief_save,
            "card.visual_generate": self._card_visual_generate,
            "card.visual_job": self._card_visual_job,
            "card.asset_save": self._card_asset_save,
            "card.asset_select": lambda p: _avatars.asset_select(str(p.get("path") or ""), str(p.get("kind") or ""), str(p.get("name") or "")),
            "card.asset_remove": lambda p: _avatars.asset_remove(str(p.get("path") or ""), str(p.get("kind") or ""), str(p.get("name") or "")),
            "card.asset_matte": lambda p: _avatars.asset_matte(str(p.get("path") or ""), str(p.get("kind") or ""), str(p.get("name") or "")),
            "card.stickers_save": self._card_stickers_save,
            "card.sticker_remove": lambda p: _avatars.sticker_remove(str(p.get("path") or ""), str(p.get("name") or "")),
            "card.sticker_rename": lambda p: _avatars.sticker_rename(str(p.get("path") or ""), str(p.get("old") or ""), str(p.get("new") or "")),
            "card.sticker_reslice": self._card_sticker_reslice,
            "card.assets_list": lambda p: _avatars.assets_list(str(p.get("path") or "")),
            "card.asset_file_upload": lambda p: _avatars.asset_file_upload(
                str(p.get("path") or ""), str(p.get("name") or ""),
                str(p.get("data_b64") or ""), str(p.get("ext") or "")),
            "card.asset_file_read": lambda p: _avatars.asset_file_read(
                str(p.get("path") or ""), str(p.get("rel") or p.get("name") or "")),
            "card.asset_file_delete": lambda p: _avatars.asset_file_delete(
                str(p.get("path") or ""), str(p.get("rel") or p.get("name") or "")),
            "card.asset_delete": lambda p: _avatars.asset_delete(str(p.get("path") or ""), str(p.get("kind") or "")),
            "card.avatar_read": lambda p: _avatars.avatar_read(str(p.get("path") or "")),
            "market.search": lambda p: _card_market.search(
                str(p.get("query") or ""),
                sort=str(p.get("sort") or "most_popular"),
                limit=int(p.get("limit") or 24),
                page=int(p.get("page") or 1),
                nsfw=bool(p.get("nsfw")),
                tags=p.get("tags"),
                oc=bool(p.get("oc")),
                lorebook=bool(p.get("lorebook")),
            ),
            "market.detail": lambda p: _card_market.detail(str(p.get("path") or "")),
            "market.import": lambda p: _card_market.import_card(
                str(p.get("path") or ""), nsfw=bool(p.get("nsfw")),
            ),
            "cards.list": lambda p: _cards.list_cards(),
            "card.read": self._card_read,
            "card.save": lambda p: _cards.save_card(p.get("data"), path=str(p.get("path") or "")),
            "card.patch": self._card_patch,
            "card.delete": lambda p: _cards.delete_card(str(p.get("path") or "")),
            "card.restore": lambda p: _cards.restore_card(str(p.get("trash_id") or "")),
            "card.duplicate": lambda p: _cards.duplicate_card(str(p.get("path") or "")),
            "card.rewrite_field": self._card_rewrite_field,
            "card.merge_world": self._card_merge_world,
            "cards.draft": self._cards_draft,
            "cards.import_foreign": lambda p: _cards.import_foreign_card(
                str(p.get("text") or ""), png_b64=str(p.get("png_b64") or "")),
            "card.from_draft": self._card_from_draft,
            "card.generate_worldbook": self._card_generate_worldbook,
            "defaults.get": lambda p: _config._public_defaults(_config.load_defaults()),
            "image.catalog": self._image_catalog,
            "defaults.set": self._defaults_set,
            "defaults.apply_key": self._defaults_apply_key,
            "key.test": self._key_test,
            "models.list": self._models_list,
            "transcribe.card": self._transcribe_card,
            "open.path": lambda p: _sessions.open_path(str(p.get("path") or ""), reveal=bool(p.get("reveal"))),
            "update.status": lambda p: _updates.status(force=bool(p.get("force"))),
            "update.apply": lambda p: _updates.apply(),
            "update.restart": self._update_restart,
        }

    # -- handlers ---------------------------------------------------------------

    def _update_restart(self, p: dict[str, Any]) -> Any:
        """Relaunch the resident instance into the just-installed code (os.execv). The
        client's WS drops and auto-reconnects to the new process. Scheduled after a short
        delay so this response flushes first. No supervisor (e.g. a foreground tui) → the
        client falls back to the manual command it already has from update.status."""
        if self.supervisor is None:
            return {"ok": False, "error": "no resident instance to restart — restart manually"}
        delay = p.get("delay")
        ok = self.supervisor.schedule_restart(float(delay) if isinstance(delay, (int, float)) else 1.0)
        return {"ok": bool(ok), "restarting": bool(ok)}

    def _hub_state(self, p: dict[str, Any]) -> Any:
        defaults = _config.load_defaults()
        sessions = [_sessions.session_entry(m, self.supervisor) for m in S.list_sessions()]
        return {
            "version": __version__,
            "first_run": not _config.desktop_config_path().exists() and not sessions,
            "defaults": _config._public_defaults(defaults),
            "presets": {k: {kk: vv for kk, vv in v.items() if kk != "api_key"} for k, v in PRESETS.items()},
            "sessions": sessions,
            "cards": _cards.list_cards(),
            "home": str(S.lunamoth_home()),
            # Distribution lock (LUNAMOTH_FORCE_SANDBOX): the UI greys the sandbox toggle.
            "force_sandbox": _isolation.force_sandbox(),
        }

    def _sessions_list(self, p: dict[str, Any]) -> Any:
        return [_sessions.session_entry(m, self.supervisor) for m in S.list_sessions()]

    def _session_start(self, p: dict[str, Any]) -> Any:
        meta = _meta(p)
        if self.supervisor is not None:
            _await_supervisor(self.supervisor, self.supervisor.start_chara(meta.name))
        elif not _sessions.start_daemon(meta):
            raise RpcError(-32033, "chara is not set up yet")
        return _sessions.session_entry(meta, self.supervisor)

    def _session_stop(self, p: dict[str, Any]) -> Any:
        meta = _meta(p)
        if self.supervisor is not None:
            _await_supervisor(self.supervisor, self.supervisor.stop_chara(meta.name))
        else:
            _sessions.stop_daemon(meta)
        return _sessions.session_entry(meta, self.supervisor)

    def _set_autonomy(self, p: dict[str, Any]) -> Any:
        # Toggle autonomous running without killing the chat you're in
        # (the in-chat 'autonomy' switch). The board's start/stop touches
        # the child; this only flips the persisted pause marker.
        meta = _meta(p)
        on = bool(p.get("on"))
        if self.supervisor is not None:
            _await_supervisor(self.supervisor, self.supervisor.set_autonomy(meta.name, on))
        else:
            from ..supervisor import Supervisor
            Supervisor.set_mode_on_disk(meta, "live" if on else "chat")
        return _sessions.session_entry(meta, self.supervisor)

    def _gateway_start(self, p: dict[str, Any]) -> Any:
        meta = _meta(p)
        if self.supervisor is None:
            raise RpcError(-32060, "gateway supervision requires lunamothd")
        return _await_supervisor(self.supervisor, self.supervisor.start_gateway(meta.name, persist=True))

    def _gateway_stop(self, p: dict[str, Any]) -> Any:
        meta = _meta(p)
        if self.supervisor is None:
            raise RpcError(-32060, "gateway supervision requires lunamothd")
        return _await_supervisor(self.supervisor, self.supervisor.stop_gateway(meta.name, persist=True))

    def _gateway_status(self, p: dict[str, Any]) -> Any:
        meta = _meta(p)
        if self.supervisor is None:
            return _sessions._gateway_status_from_disk(meta)
        # Live query: ask the in-child host whether it is actually running,
        # waiting for a QR (needs_login), or stopped — not a heuristic.
        return _await_supervisor(self.supervisor, self.supervisor.gateway_status_live(meta.name))

    def _gateways_list(self, p: dict[str, Any]) -> Any:
        # Global gateway view: live status for every chara, one source of
        # truth shared with the per-chara panel.
        if self.supervisor is None:
            return {"gateways": [
                {"name": m.name, "enabled": bool((_sessions._read_messaging(m) or {}).get("enabled")),
                 "gateway": _sessions._gateway_status_from_disk(m)}
                for m in S.list_sessions()
            ]}
        return _await_supervisor(self.supervisor, self.supervisor.gateways_all_live())

    def _superchat_read(self, p: dict[str, Any]) -> Any:
        meta = _meta(p)
        return _sessions.set_superchat_read(meta, float(p.get("ts") or 0.0))

    def _session_delete(self, p: dict[str, Any]) -> Any:
        meta = _meta(p)
        if p.get("confirm") != meta.name:
            raise RpcError(-32034, "confirmation text does not match")
        if self.supervisor is not None:
            _await_supervisor(self.supervisor, self.supervisor.stop_chara(meta.name))
            _await_supervisor(self.supervisor, self.supervisor.stop_gateway(meta.name, persist=False))
        else:
            _sessions.stop_daemon(meta)
        S.delete_session(meta.name)
        return {"ok": True}

    def _session_export(self, p: dict[str, Any]) -> Any:
        return _sessions.export_session(_meta(p))

    def _session_wake(self, p: dict[str, Any]) -> Any:
        # If a visual is still generating for this card, waking now would freeze a copy
        # that's missing the in-flight image — warn unless the user chose to wake anyway.
        from ...visuals import jobs
        card = str(p.get("card") or "")
        if not bool(p.get("force")) and jobs.running_for(card) > 0:
            raise HubRpcError(-32050, "a visual is still generating for this card — wait or wake anyway",
                              {"kind": "visual_in_flight"})
        cd = p.get("card_data")
        return _sessions.wake(
            card_path=str(p.get("card") or ""),
            name=str(p.get("name") or ""),
            isolation=str(p.get("isolation") or "sandbox"),
            model=str(p.get("model") or ""),
            toolpack=str(p.get("toolpack") or ""),
            embodiment=str(p.get("embodiment") or ""),
            website=str(p.get("website") or ""),
            key=str(p.get("key") or ""),
            mode=str(p.get("mode") or "live"),
            network=bool(p.get("network", True)),
            card_data=cd if isinstance(cd, dict) else None,
        )

    def _session_set_modules(self, p: dict[str, Any]) -> Any:
        return _sessions.set_modules(
            _meta(p),
            force_roleplay=p.get("force_roleplay"),
            website=p.get("website"),
        )

    def _chara_set_isolation(self, p: dict[str, Any]) -> Any:
        return _sessions.set_isolation(_meta(p), str(p.get("isolation") or ""))

    def _chara_running(self, meta: "S.SessionMeta") -> bool:
        if self.supervisor is not None:
            st = self.supervisor.chara_status(meta.name)
            return bool(st and st.get("state") == "running")
        return meta.status() in ("attached", "running")

    def _card_patch(self, p: dict[str, Any]) -> Any:
        # Field-level merge writer (deck OR a living chara's session card). When the
        # edited card belongs to a RUNNING chara, flag it dirty so the UI offers 立即应用
        # (the soul rides the cache-stable prefix → it only re-reads on (re)start).
        path = str(p.get("path") or "")
        out = _cards.patch_card(path, p.get("fields") or {})
        meta = _sessions.session_for_card(path)
        if meta is not None and self._chara_running(meta):
            _sessions.mark_card_dirty(meta)
            out["card_dirty"] = True
        return out

    def _chara_apply_card(self, p: dict[str, Any]) -> Any:
        # Apply a pending card edit to a running chara = restart its child (history is
        # restored by make_session). No resident supervisor → just clear the flag; the
        # next start reads the new card.
        meta = _meta(p)
        if self.supervisor is None:
            _sessions.clear_card_dirty(meta)
            return {"ok": True, "restarted": False, "applies": "next_start"}
        res = _await_supervisor(self.supervisor, self.supervisor.restart_chara(meta.name))
        # A successful restart already cleared card_dirty inside child.start() (right after
        # the fresh process launched). Do NOT clear again here: a card.patch that lands in
        # the window after start() would re-flag a genuinely-unapplied edit, and a blind
        # second clear would wipe that "待应用" intent. Only clear when nothing restarted.
        if not (isinstance(res, dict) and res.get("restarted")):
            _sessions.clear_card_dirty(meta)
        return {"ok": True, **(res if isinstance(res, dict) else {})}

    def _card_visual_jobs(self, p: dict[str, Any]) -> Any:
        from ...visuals import jobs
        return {"running": jobs.running_for(str(p.get("path") or ""))}

    def _keys_save(self, p: dict[str, Any]) -> Any:
        return _config.save_key(str(p.get("label") or ""), provider=str(p.get("provider") or ""),
                                base_url=str(p.get("base_url") or ""), api_key=str(p.get("api_key") or ""),
                                model=str(p.get("model") or ""))

    def _matte_status(self, p: dict[str, Any]) -> Any:
        from ...visuals import matte
        return matte.status()

    def _matte_install_deps(self, p: dict[str, Any]) -> Any:
        from ...visuals import matte
        matte.install_deps_async()
        return matte.status()

    def _matte_download(self, p: dict[str, Any]) -> Any:
        # Installs the matte model in the background: the matting engine
        # (rembg/onnxruntime) first if it isn't present, then the weights — one
        # click, no separate deps step. Progress is polled via matte.status.
        from ...visuals import matte
        mid = str(p.get("model") or "")
        if mid not in matte.MODELS:
            raise RpcError(-32602, f"unknown matte model: {mid}")
        matte.download_async(mid)
        return matte.status()

    def _matte_delete(self, p: dict[str, Any]) -> Any:
        from ...visuals import matte
        matte.delete(str(p.get("model") or ""))
        return matte.status()

    def _matte_use(self, p: dict[str, Any]) -> Any:
        from ...visuals import matte
        mid = str(p.get("model") or "")
        if mid not in matte.MODELS:
            raise RpcError(-32602, f"unknown matte model: {mid}")
        _config.save_defaults({"matte_model": mid})
        return matte.status()

    def _card_avatar_upload(self, p: dict[str, Any]) -> Any:
        return _avatars.avatar_upload(str(p.get("path") or ""), str(p.get("data_b64") or ""),
                                      str(p.get("ext") or ""))

    def _card_visual_brief(self, p: dict[str, Any]) -> Any:
        # The visual brief is PERSISTED on the card (extensions.lunamoth.visual_brief)
        # so viewing/reusing it never re-pays the LLM. Return the stored brief unless a
        # rebuild is explicitly requested (force); a fresh build is persisted too.
        from ...visuals import pipeline
        path = str(p.get("path") or "")
        force = bool(p.get("force"))
        try:
            card = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RpcError(-32035, f"unreadable card: {exc}") from exc
        stored = _brief_of(card)
        if stored and not force:
            return {"brief": stored, "stored": True}
        defaults = _config.load_defaults()
        _vp_model = str(defaults.get("image_prompt_model") or "")
        # The image-prompt model runs on its OWN provider when set, else the main default.
        defaults = _config.task_defaults(defaults, str(defaults.get("image_prompt_provider") or ""))
        try:
            brief = pipeline.build_brief(
                card, lambda s, u: _pkg()._complete(defaults, s, u, model=_vp_model, temperature=0.7))
        except (RuntimeError, ValueError) as exc:
            raise HubRpcError(-32050, str(exc), {"kind": "visual_brief"}) from exc
        try:
            _avatars.visual_brief_save(path, brief)  # best-effort (builtin/PNG aren't writable)
        except RpcError:
            pass
        return {"brief": brief, "stored": False}

    def _card_visual_brief_save(self, p: dict[str, Any]) -> Any:
        brief = p.get("brief")
        if not isinstance(brief, dict):
            raise RpcError(-32602, "card.visual_brief_save expects a brief object")
        return _avatars.visual_brief_save(str(p.get("path") or ""), brief)

    def _card_visual_generate(self, p: dict[str, Any]) -> Any:
        # Async + AUTO-SAVE: generation is SLOW (30–240 s) AND must survive the user
        # leaving the card view, so the job generates THEN writes the result straight
        # to the card (avatar_upload / asset_save / stickers_save). The client polls
        # card.visual_job only for progress; the card is updated regardless. The brief
        # is reused-then-persisted, and the saved keyvisual is the server-side identity
        # anchor for the other kinds. Returns immediately with a job_id.
        from ...visuals import jobs, pipeline
        path = str(p.get("path") or p.get("card_path") or "")
        kind = str(p.get("kind") or "avatar")
        if kind not in pipeline.KINDS:
            raise RpcError(-32602, f"unknown visual kind: {kind} "
                           f"(one of {', '.join(pipeline.KINDS)})")
        try:
            card = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RpcError(-32035, f"unreadable card: {exc}") from exc
        defaults = _config.load_defaults()
        matte_opt = p.get("matte")
        brief_in = p.get("brief") if isinstance(p.get("brief"), dict) else _brief_of(card)
        extra = str(p.get("extra") or "")  # optional per-generation steer (额外提示词)
        grid = _norm_grid(p.get("grid"))   # stickers: 1x1 / 2x2 / 3x3 (others ignore it)
        refs_in = [str(r) for r in p.get("refs")] if isinstance(p.get("refs"), list) else []
        if kind != "keyvisual":  # identity-lock: anchor the rest to the saved keyvisual
            anchor = _keyvisual_data_uri(path, card)
            if anchor:
                refs_in = [anchor, *refs_in]
        refs_in = refs_in or None

        def _run() -> dict[str, Any]:
            out = pipeline.generate(
                card, kind,
                llm_call=lambda s, u: _pkg()._complete(defaults, s, u, temperature=0.7),
                brief=brief_in,
                refs=refs_in,
                extra=extra,
                grid=grid,
                matte=(None if matte_opt is None else bool(matte_opt)),
            )
            try:
                _avatars.visual_brief_save(path, out["brief"])  # persist (best-effort)
            except RpcError:
                pass
            # AUTO-SAVE to the card so the result is kept even if the client navigated away.
            if "stickers" in out:
                cells = [base64.b64encode(c).decode("ascii") for c in out["stickers"]]
                sheet_b64 = base64.b64encode(out["sheet"]).decode("ascii") if out.get("sheet") else None
                saved = _avatars.stickers_save(path, cells, names=out.get("names"), sheet=sheet_b64)
                return {"saved": True, "kind": kind, "urls": saved["urls"], "added": saved.get("added"),
                        "sheet_urls": saved.get("sheet_urls"),
                        "note": out["note"], "matted": bool(out.get("matted"))}
            data = out["data"]
            if kind == "avatar":
                from ...content import imaging as _imaging
                small = _imaging.compress_image_bytes(data, "png", _imaging.CAP_AVATAR)
                saved = _avatars.avatar_upload(path, base64.b64encode(small).decode("ascii"), "png")
                return {"saved": True, "kind": kind, "data_uri": saved["data_uri"],
                        "url": saved.get("url"), "options": saved.get("options"),
                        "note": out["note"], "matted": bool(out.get("matted"))}
            saved = _avatars.asset_save(path, kind, base64.b64encode(data).decode("ascii"), out["ext"])
            return {"saved": True, "kind": kind, "url": saved["url"], "note": out["note"], "matted": bool(out.get("matted"))}

        return {"status": "running",
                "job_id": jobs.submit(_run, label=f"visual:{kind}", meta={"path": path})}

    def _card_visual_job(self, p: dict[str, Any]) -> Any:
        # Poll a card.visual_generate job. running → {status:"running"}; ready →
        # {status:"ready", ...payload}; a real failure surfaces as a structured error;
        # unknown = the id expired/was never seen (client stops polling gracefully).
        from ...visuals import jobs
        jid = str(p.get("job_id") or "")
        if not jid:
            raise RpcError(-32602, "card.visual_job needs a job_id")
        st = jobs.status(jid)
        if st["status"] == "ready":
            return {"status": "ready", **(st.get("result") or {})}
        if st["status"] == "failed":
            raise HubRpcError(-32050, st.get("error") or "image generation failed",
                              {"kind": "visual_generate"})
        return {"status": st["status"]}  # running | unknown

    def _card_asset_save(self, p: dict[str, Any]) -> Any:
        return _avatars.asset_save(str(p.get("path") or ""), str(p.get("kind") or ""),
                                   str(p.get("data_b64") or ""), str(p.get("ext") or ""))

    def _card_stickers_save(self, p: dict[str, Any]) -> Any:
        items = p.get("data_b64")
        if not isinstance(items, list):
            raise RpcError(-32602, "card.stickers_save expects data_b64: a list of PNG base64 cells")
        names = p.get("names") if isinstance(p.get("names"), list) else None
        return _avatars.stickers_save(str(p.get("path") or ""), [str(x) for x in items],
                                      names=[str(n) for n in names] if names else None)

    def _card_sticker_reslice(self, p: dict[str, Any]) -> Any:
        # Re-cut a KEPT raw sheet into the chosen grid and APPEND the cells (a wrong
        # auto-slice is recoverable without re-paying for generation). Local-only
        # (slice + matte), so synchronous.
        from ...visuals import pipeline
        path = str(p.get("path") or "")
        sheet_name = str(p.get("sheet") or p.get("name") or "").strip()
        target = _avatars._writable_card_path(path)  # raises on builtin/PNG/non-writable
        # only an actual kept sheet (`<stem>.sticker_sheet.<id>.<ext>`) — defense in depth
        # so a stray/mistyped name can't be fed to the slicer.
        if ".sticker_sheet." not in sheet_name:
            raise RpcError(-32602, f"no such sticker sheet: {sheet_name}")
        sheet_file = _avatars._rel(target, sheet_name)  # subdir-safe, like the other asset paths
        if not sheet_name or not sheet_file.is_file():
            raise RpcError(-32602, f"no such sticker sheet: {sheet_name}")
        rows, cols = _norm_grid(p.get("grid")) or pipeline.KINDS["stickers"]["grid"]
        cells, _matted, note = pipeline._slice_and_cut(sheet_file.read_bytes(), rows, cols, True)
        b64 = [base64.b64encode(c).decode("ascii") for c in cells]
        saved = _avatars.stickers_save(str(target), b64, names=pipeline.sticker_default_names(len(cells)))
        return {**saved, "note": note}

    def _card_read(self, p: dict[str, Any]) -> Any:
        path = Path(str(p.get("path") or ""))
        try:
            card = _cards.CharacterCard.load(path)
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
                "language": card.language, "extensions": _cards._safe_extensions_for_ui(card.extensions),
                "character_book": _cards._book_to_dict(card.character_book),
                "raw": raw}

    def _card_rewrite_field(self, p: dict[str, Any]) -> Any:
        return _card_draft.rewrite_card_field(_config.load_defaults(), field=str(p.get("field") or ""),
                                              value=str(p.get("value") or ""),
                                              instruction=str(p.get("instruction") or ""),
                                              context=str(p.get("context") or ""),
                                              model=str(p.get("model") or ""))

    def _card_merge_world(self, p: dict[str, Any]) -> Any:
        return _cards.merge_world(str(p.get("card_path") or p.get("path") or ""), p.get("world"))

    def _cards_draft(self, p: dict[str, Any]) -> Any:
        inspiration = str(p.get("inspiration") or "").strip()
        if not inspiration:
            raise RpcError(-32602, "cards.draft needs inspiration")
        # Card drafting uses the per-task card_model + card_provider when set, else
        # the system default (Settings · 模型 · 其他模态 · 生成角色卡). The model runs
        # on its OWN provider — same per-task-provider pattern as read-image.
        _d = _config.load_defaults()
        _route = _config.task_defaults(_d, str(_d.get("card_provider") or ""))
        return _card_draft.draft_card_from_inspiration(_route, inspiration, model=str(_d.get("card_model") or ""))

    def _card_from_draft(self, p: dict[str, Any]) -> Any:
        draft = p.get("draft")
        if not isinstance(draft, dict):
            raise RpcError(-32602, "card.from_draft expects a draft object")
        return _cards.save_card(
            _card_draft.draft_to_card(draft, origin_text=str(p.get("origin") or ""), as_draft=bool(p.get("as_draft"))),
            path=str(p.get("path") or ""),
        )

    def _card_generate_worldbook(self, p: dict[str, Any]) -> Any:
        # Generate / expand a card's world book from its persona — same per-task
        # card_model + card_provider as cards.draft (Settings · 模型 · 其他模态).
        _d = _config.load_defaults()
        _route = _config.task_defaults(_d, str(_d.get("card_provider") or ""))
        raw_count = p.get("count")
        count = int(raw_count) if isinstance(raw_count, (int, float)) else 8
        return _card_draft.generate_worldbook(
            _route,
            name=str(p.get("name") or ""),
            description=str(p.get("description") or ""),
            personality=str(p.get("personality") or ""),
            scenario=str(p.get("scenario") or ""),
            first_mes=str(p.get("first_mes") or ""),
            existing=p.get("existing") if isinstance(p.get("existing"), list) else [],
            mode=str(p.get("mode") or "fresh"),
            count=count,
            model=str(_d.get("card_model") or ""),
        )

    def _image_catalog(self, p: dict[str, Any]) -> Any:
        # The image-gen provider catalogue for Settings · 模型 / 提供商: each
        # provider with its selectable models + whether a usable key is set
        # (reusing the named provider keyring), and which one is active.
        # OpenRouter additionally gets its LIVE image-output models grafted on
        # top of the curated picks (its /models lists 9 image models — though
        # not grok-imagine, which is why curated picks stay).
        def _image_models(pid: str, base: str, key: str) -> list[dict]:
            if pid != "openrouter":
                return []
            out = []
            for m in _models._catalogue(base, key):
                arch = m.get("architecture") or {}
                if "image" in (arch.get("output_modalities") or []):
                    out.append({"id": m.get("id"), "label": m.get("name") or m.get("id")})
            return out
        return {"providers": _config.image_providers.catalogue(_config._read_desktop_raw(), _image_models)}

    def _defaults_set(self, p: dict[str, Any]) -> Any:
        # api_key is not a default field (the keyring is the one store), so it's
        # filtered out of `updates` here and can never be written top-level.
        updates = {k: v for k, v in p.items() if k in _config._DEFAULT_FIELDS and isinstance(v, str)}
        defaults = _config.save_defaults(updates)
        public = _config._public_defaults(defaults)
        public["key_update_candidates"] = []  # obsolete since SEC-2 (kept for client contract)
        return public

    def _defaults_apply_key(self, p: dict[str, Any]) -> Any:
        names = p.get("names")
        if not isinstance(names, list):
            raise RpcError(-32602, "defaults.apply_key expects names: [...]")
        return _config.apply_default_key([str(n) for n in names])

    def _key_test(self, p: dict[str, Any]) -> Any:
        # A `label` tests a SPECIFIC saved provider key (the 提供商 pane's per-row
        # test) by resolving its stored secret server-side; otherwise we test the
        # active default (the Model pane's Test button).
        label = str(p.get("label") or "").strip()
        if label:
            rec = _config.resolve_key(label)
            if rec is None:
                return {"ok": False, "error": {"kind": "config", "detail": f"no saved key '{label}'"}}
            return _models.test_key(
                provider=rec["provider"], base_url=rec["base_url"],
                api_key=rec["api_key"], model=rec["model"],
            )
        defaults = _config.load_defaults()
        return _models.test_key(
            provider=str(p.get("provider") or defaults.get("provider", "")),
            base_url=str(p.get("base_url") or defaults.get("base_url", "")),
            api_key=str(p.get("api_key") or _config.active_key()),
            model=str(p.get("model") or defaults.get("model", "")),
        )

    def _models_list(self, p: dict[str, Any]) -> Any:
        # Returns {models, stale}: stale=true means the provider's /models couldn't be
        # reached, so this is a cached-but-old or curated FALLBACK list (the UI says so
        # rather than presenting a guess as live). _catalogue_meta never raises.
        defaults = _config.load_defaults()
        base = str(p.get("base_url") or defaults.get("base_url", ""))
        key = str(p.get("api_key") or _config.active_key())
        models, source = _models._catalogue_meta(base, key)
        out = []
        for m in models:
            params = m.get("supported_parameters") or []
            arch = m.get("architecture") or {}
            out.append({
                "id": m.get("id"), "name": m.get("name") or m.get("id"),
                "context": m.get("context_length"),
                "tools": ("tools" in params) if params else None,
                "vision": "image" in (arch.get("input_modalities") or []),
                "writing": any(s in str(m.get("id", "")).lower() for s in _models._WRITING_STAR),
            })
        return {"models": out, "stale": source != "fresh"}

    def _transcribe_card(self, p: dict[str, Any]) -> Any:
        text = str(p.get("text") or "").strip()
        if not text:
            raise RpcError(-32602, "transcribe.card needs text")
        # Transcribe shares card drafting's per-task model + provider (same task).
        _d = _config.load_defaults()
        _route = _config.task_defaults(_d, str(_d.get("card_provider") or ""))
        return _card_draft.transcribe_card(_route, text, model=str(_d.get("card_model") or ""))

    @staticmethod
    def _meta(p: dict[str, Any]) -> S.SessionMeta:
        return _meta(p)
