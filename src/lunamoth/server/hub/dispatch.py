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
from ...session import sessions as S
from ...session.settings import PRESETS
from ..dispatch import RpcError, error_response, ok_response, _normalize_request
from . import avatars as _avatars
from . import card_draft as _card_draft
from . import cards as _cards
from . import config as _config
from . import models as _models
from . import sessions as _sessions
from ._common import HubRpcError, _await_supervisor, _meta

_log = logging.getLogger("lunamoth.server.hub")


def _pkg():
    from .. import hub
    return hub


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
            "card.visual_generate": self._card_visual_generate,
            "card.visual_job": self._card_visual_job,
            "card.asset_save": self._card_asset_save,
            "card.stickers_save": self._card_stickers_save,
            "card.asset_delete": lambda p: _avatars.asset_delete(str(p.get("path") or ""), str(p.get("kind") or "")),
            "card.avatar_read": lambda p: _avatars.avatar_read(str(p.get("path") or "")),
            "cards.list": lambda p: _cards.list_cards(),
            "card.read": self._card_read,
            "card.save": lambda p: _cards.save_card(p.get("data"), path=str(p.get("path") or "")),
            "card.delete": lambda p: _cards.delete_card(str(p.get("path") or "")),
            "card.restore": lambda p: _cards.restore_card(str(p.get("trash_id") or "")),
            "card.duplicate": lambda p: _cards.duplicate_card(str(p.get("path") or "")),
            "card.rewrite_field": self._card_rewrite_field,
            "card.merge_world": self._card_merge_world,
            "cards.draft": self._cards_draft,
            "card.from_draft": self._card_from_draft,
            "defaults.get": lambda p: _config._public_defaults(_config.load_defaults()),
            "image.catalog": self._image_catalog,
            "defaults.set": self._defaults_set,
            "defaults.apply_key": self._defaults_apply_key,
            "key.test": self._key_test,
            "models.list": self._models_list,
            "transcribe.card": self._transcribe_card,
            "open.path": lambda p: _sessions.open_path(str(p.get("path") or ""), reveal=bool(p.get("reveal"))),
        }

    # -- handlers ---------------------------------------------------------------

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
        # R9: build (only) the visual brief for a card via the GLOBAL default
        # text model — the UI shows/edits it, then reuses it across the set so
        # "generate all" pays for ONE brief, not one per asset.
        from ...visuals import pipeline
        try:
            card = json.loads(Path(str(p.get("path") or "")).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RpcError(-32035, f"unreadable card: {exc}") from exc
        defaults = _config.load_defaults()
        _vp_model = str(defaults.get("image_prompt_model") or "")
        # The image-prompt model runs on its OWN provider when set, else the main
        # default (same per-task-provider pattern as read-image / card draft).
        defaults = _config.task_defaults(defaults, str(defaults.get("image_prompt_provider") or ""))
        try:
            return {"brief": pipeline.build_brief(
                card, lambda s, u: _pkg()._complete(defaults, s, u, model=_vp_model, temperature=0.7, max_tokens=3000))}
        except (RuntimeError, ValueError) as exc:
            raise HubRpcError(-32050, str(exc), {"kind": "visual_brief"}) from exc

    def _card_visual_generate(self, p: dict[str, Any]) -> Any:
        # R9 (async): card → brief (GLOBAL default text model, or a reused one) →
        # Seedream image (optionally guided by user refs / a generated anchor) →
        # optional matte → preview bytes. Generation is SLOW (30–240 s), so this
        # returns immediately with a job_id; the client polls card.visual_job. The
        # result bytes are returned for the UI to show/save (avatars via
        # avatar_upload, single art via asset_save, the sticker set via stickers_save).
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
        brief_in = p.get("brief") if isinstance(p.get("brief"), dict) else None
        refs_in = [str(r) for r in p.get("refs")] if isinstance(p.get("refs"), list) else None

        def _run() -> dict[str, Any]:
            out = pipeline.generate(
                card, kind,
                llm_call=lambda s, u: _pkg()._complete(defaults, s, u, temperature=0.7, max_tokens=3000),
                brief=brief_in,
                refs=refs_in,
                matte=(None if matte_opt is None else bool(matte_opt)),
            )
            common = {"mime": out["mime"], "ext": out["ext"], "kind": out["kind"],
                      "matted": out["matted"], "note": out["note"], "brief": out["brief"]}
            if "stickers" in out:  # a sliced set → a list of base64 cells
                return {"stickers": [base64.b64encode(c).decode("ascii") for c in out["stickers"]],
                        **common}
            return {"data_b64": base64.b64encode(out["data"]).decode("ascii"), **common}

        return {"status": "running", "job_id": jobs.submit(_run, label=f"visual:{kind}")}

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
        return _avatars.stickers_save(str(p.get("path") or ""), [str(x) for x in items])

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
        updates = {k: v for k, v in p.items() if k in _config._DEFAULT_FIELDS and isinstance(v, str)}
        before = _config.load_defaults()
        defaults = _config.save_defaults(updates)
        public = _config._public_defaults(defaults)
        changed_key = "api_key" in updates and updates.get("api_key") != before.get("api_key")
        if changed_key and defaults.get("api_key"):
            public["key_update_candidates"] = _config.key_update_candidates(defaults)
        else:
            public["key_update_candidates"] = []
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
            api_key=str(p.get("api_key") or defaults.get("api_key", "")),
            model=str(p.get("model") or defaults.get("model", "")),
        )

    def _models_list(self, p: dict[str, Any]) -> Any:
        defaults = _config.load_defaults()
        base = str(p.get("base_url") or defaults.get("base_url", ""))
        key = str(p.get("api_key") or defaults.get("api_key", ""))
        try:
            models = _models._catalogue(base, key)
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
                "writing": any(s in str(m.get("id", "")).lower() for s in _models._WRITING_STAR),
            })
        return out

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
