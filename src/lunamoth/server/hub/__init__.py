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

This is a PACKAGE split by domain (config / models / cards / sessions /
dispatch). Every name that was public on the old flat module is re-exported here
so the historic ``from ..server import hub as H`` contract is byte-identical —
including the stdlib modules and class re-exports the test-suite reaches for
(``H.subprocess``, ``H.Path``, ``H.CharacterCard``, ``H._http_json`` …). The
``_complete`` / ``_http_json`` helpers are looked up off this package at call
time by their internal callers, so a test patching ``H._complete`` is honored
across the submodules.
"""
from __future__ import annotations

# Stdlib names some callers/tests reach via the module namespace (H.subprocess
# patched for Popen; H.Path patched for Path.home).
import subprocess  # noqa: F401 - re-exported on the package surface
from pathlib import Path  # noqa: F401 - re-exported on the package surface

from ... import __version__  # noqa: F401

# Class re-exports kept on the package surface (H.CharacterCard, H.CAP_ART).
from ...content.cards import CharacterCard  # noqa: F401
from ...content.imaging import CAP_ART  # noqa: F401

from ._common import (  # noqa: F401
    HubRpcError,
    _asset_url,
    _atomic_write_json,
    _await_supervisor,
    _clean_theme,
    _clean_theme_color,
    _meta,
    _sanitize_avatar_svg,
    _slug,
    _writable_card_path,
)
from .config import (  # noqa: F401
    _DEFAULT_FIELDS,
    _SECRET_FIELDS,
    _base_url_id,
    _config_matches_model_route,
    _key_overrides,
    _keys_map,
    _provider_id,
    _public_defaults,
    _read_desktop_raw,
    _write_desktop_raw,
    apply_default_key,
    bundled_cards_dir,
    delete_key,
    desktop_config_path,
    key_update_candidates,
    list_keys,
    load_defaults,
    save_defaults,
    save_key,
    use_key,
    user_cards_dir,
    user_worlds_dir,
)
from .config import image_providers  # noqa: F401
from .models import (  # noqa: F401
    _HTTP_TIMEOUT,
    _WRITING_STAR,
    _catalogue,
    _classify_http_error,
    _complete,
    _http_error_detail,
    _http_json,
    _models_cache,
    model_capabilities,
    test_key,
)
from .avatars import (  # noqa: F401
    _art_sidecar_path,
    _avatar_data_uri,
    _avatar_thumb_uri,
    _looks_like,
    asset_delete,
    asset_save,
    avatar_read,
    avatar_upload,
)
from .card_draft import (  # noqa: F401
    _draft_polaris,
    _draft_world_entries,
    _invalid_draft,
    _parse_card_draft,
    _strip_text_fence,
    _theme_color,
    _validate_polaris,
    _validate_user_name,
    _validate_world_entries,
    draft_card_from_inspiration,
    draft_to_card,
    generate_worldbook,
    rewrite_card_field,
    transcribe_card,
)
from .cards import (  # noqa: F401
    _book_to_dict,
    _card_entry,
    _card_sources,
    _copy_card_assets,
    _iter_card_files,
    _merge_preserving,
    _safe_extensions_for_ui,
    _sanitize_card_extensions,
    _session_card_entry,
    _trash_cards_dir,
    delete_card,
    duplicate_card,
    list_cards,
    merge_world,
    restore_card,
    save_card,
    store_upload,
)
from .sessions import (  # noqa: F401
    _SECRET_MASK,
    _WORK_READ_CAP,
    _gateway_status_from_disk,
    _last_error,
    _mask_secrets,
    _merge_messaging,
    _read_config,
    _read_messaging,
    _read_optional,
    _speak_texts_from_struct,
    _transcript_export_jsonl,
    _transcript_speaks,
    _unmask_secrets,
    _weixin_config,
    _write_home_scaffold,
    board_error_kind,
    chara_extras,
    ensure_weixin_adapter,
    export_session,
    list_toolpacks,
    list_works,
    messaging_get,
    messaging_save,
    open_path,
    read_work,
    session_entry,
    set_modules,
    set_superchat_read,
    start_daemon,
    stop_daemon,
    superchat_read_ts,
    superchat_unread,
    wake,
    weixin_qr,
    weixin_qr_status,
)
from .dispatch import HubDispatcher  # noqa: F401
