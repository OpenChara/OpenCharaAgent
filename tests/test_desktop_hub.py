"""Desktop hub gateway: roster RPC, wake/freeze, defaults, drafts (server/hub.py).

Everything runs against a temp LUNAMOTH_HOME; no network, no LLM (provider
HTTP paths are exercised separately / mocked here)."""
import json
import sqlite3
from pathlib import Path

import pytest

from lunamoth.server import hub as H
from lunamoth.session import sessions as S


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    yield tmp_path / "home"


def dispatch(method, params=None):
    out = []
    d = H.HubDispatcher(lambda f: out.append(f) or True)
    resp = d.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return resp


def result(method, params=None):
    resp = dispatch(method, params)
    assert "error" not in resp, resp.get("error")
    return resp["result"]


def rpc_error(method, params=None):
    resp = dispatch(method, params)
    assert "error" in resp, resp
    return resp["error"]


def luna_card_path():
    return str(H.bundled_cards_dir() / "LunaMoth" / "card.json")


def set_defaults():
    # The keyring is the ONE key store: seed a default provider key + activate it (the
    # product's provider→key→model path), not a legacy top-level api_key.
    H.save_key("default", provider="openrouter", base_url="https://example.invalid/v1",
               api_key="sk-test", model="test/model")
    H.use_key("default")


def draft_payload():
    return {
        "name": "Aster",
        "user_name": "visitor",
        "description": "Aster is a lantern keeper from a quiet orbital garden. "
        "They speak gently, collect small impossible weather signs, and keep careful notes for visitors.",
        "personality": "Gentle, patient, quietly curious.",
        "scenario": "Dusk in the orbital garden; the lanterns are waking.",
        "first_mes": "The lanterns are awake. Did you bring a question for the dark?",
        "world_entries": [
            {"keys": ["Orbital Garden"], "content": "A ring habitat where seasons are tuned by old mirrors.", "constant": True},
            {"keys": ["Lantern Archive"], "content": "Aster's catalogue of weather, omens, and names.", "constant": False},
        ],
        "polaris": "Map the whole mirror-season drift and welcome every careful visitor the dark sends",
        "tagline": "A gentle keeper of orbital lanterns",
        "theme_color": "#7c5cff",
    }


def mock_completion(monkeypatch, content, seen=None):
    def fake_http(url, api_key="", payload=None, timeout=H._HTTP_TIMEOUT):
        if seen is not None:
            seen.append({"url": url, "api_key": api_key, "payload": payload, "timeout": timeout})
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(H, "_http_json", fake_http)


# ---- hub.state & defaults -----------------------------------------------------

def test_state_first_run_and_cards():
    r = result("hub.state")
    assert r["first_run"] is True
    assert r["sessions"] == []
    names = {c["name"] for c in r["cards"]}
    assert "LunaMoth" in names  # bundled deck is visible
    assert r["defaults"]["has_key"] is False


def test_defaults_never_echo_the_key():
    set_defaults()
    r = result("defaults.get")
    assert r["has_key"] is True
    assert "api_key" not in r
    raw = json.loads(H.desktop_config_path().read_text(encoding="utf-8"))
    assert raw["keys"]["default"]["api_key"] == "sk-test"  # stored in the keyring, never echoed
    assert "api_key" not in raw  # no top-level secret copy — the keyring is the one store


def test_defaults_set_ignores_unknown_fields():
    r = result("defaults.set", {"provider": "openrouter", "evil": "x", "ui_lang": "zh"})
    assert r["provider"] == "openrouter"
    assert "evil" not in r


def test_key_rotation_is_obsolete_after_sec2():
    """SEC-2: the provider key is resolved globally at load, never copied into a
    session config. So waking embeds no key, defaults.set reports no rotation
    candidates, and defaults.apply_key is a harmless no-op."""
    set_defaults()  # keyring is the one store (provider key + active_key_label), no top-level api_key
    a = result("session.wake", {"card": luna_card_path(), "name": "same"})
    same = S.load_session(a["name"])
    cfg = json.loads(same.config_path.read_text(encoding="utf-8"))
    assert not cfg.get("api_key")  # the secret is NOT in the session config

    saved = result("defaults.set", {"api_key": "new-key"})
    assert saved["key_update_candidates"] == []  # nothing needs per-session rotation
    # defaults.set can NEVER persist a top-level secret — the keyring is the one store.
    raw = json.loads(H.desktop_config_path().read_text(encoding="utf-8"))
    assert "api_key" not in raw

    applied = result("defaults.apply_key", {"names": [same.name]})
    assert applied == {"updated": [], "skipped": [], "candidates": []}
    # still no embedded key after a rotate attempt; unrelated fields intact
    reread = json.loads(same.config_path.read_text(encoding="utf-8"))
    assert not reread.get("api_key")
    assert reread["character_path"].endswith("card.json")


def test_apply_key_validates_names_param():
    err = rpc_error("defaults.apply_key", {"names": "not a list"})
    assert err["code"] == -32602


# ---- wake (instantiation) ------------------------------------------------------

def test_wake_freezes_card_and_writes_config():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path(), "isolation": "sandbox"})
    assert entry["char_name"] == "LunaMoth"
    assert entry["status"] == "idle"
    meta = S.load_session(entry["name"])
    assert meta is not None
    frozen = meta.root / "card.json"
    assert frozen.exists()
    assert (meta.root / "card_source").read_text(encoding="utf-8") == luna_card_path()
    cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
    assert cfg["character_path"] == str(frozen)
    assert not cfg.get("api_key")  # SEC-2: the secret is NOT copied into the session config
    assert cfg["toolpack"] == "sandbox"  # from the card's extensions.lunamoth
    assert cfg["isolation"] == "sandbox"  # the one isolation field (the jail is derived from it)
    assert "py_backend" not in cfg  # no derived mirror copy in config — isolation is the source


def test_wake_with_card_data_freezes_the_edited_card_not_the_source():
    set_defaults()
    edited = {"data": {"name": "LunaMoth", "description": "EDITED AT WAKE",
                       "extensions": {"lunamoth": {"toolpack": "sandbox"}}}}
    entry = result("session.wake", {"card": luna_card_path(), "card_data": edited})
    meta = S.load_session(entry["name"])
    frozen = json.loads((meta.root / "card.json").read_text(encoding="utf-8"))
    assert frozen["data"]["description"] == "EDITED AT WAKE"   # the edit was frozen
    # the source template on disk is untouched
    source = json.loads(Path(luna_card_path()).read_text(encoding="utf-8"))
    assert source["data"]["description"] != "EDITED AT WAKE"


def test_list_cards_includes_living_chara_cards_as_locked():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path(), "name": "lockcheck"})
    cards = result("cards.list")
    owned = [c for c in cards if c.get("owner") == entry["name"]]
    assert len(owned) == 1
    assert owned[0]["locked"] is True
    # the bundled template is still present and NOT locked (re-wakeable)
    template = next(c for c in cards if c["builtin"] and c["name"] == "LunaMoth" and not c.get("locked"))
    assert template["locked"] is False


def test_wake_without_model_config_is_refused():
    err = rpc_error("session.wake", {"card": luna_card_path()})
    assert "no model configured" in err["message"]


def test_wake_embodiment_is_a_wake_time_choice_persisted_in_config():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path(), "embodiment": "actor"})
    meta = S.load_session(entry["name"])
    cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
    assert cfg["embodiment_override"] == "actor"

    # Absent param: no override lands — the chain stays card > literal.
    plain = result("session.wake", {"card": luna_card_path()})
    plain_cfg = json.loads(S.load_session(plain["name"]).config_path.read_text(encoding="utf-8"))
    assert plain_cfg.get("embodiment_override", "") == ""


def test_wake_lays_down_neutral_home_scaffold():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    index = meta.sandbox_dir / "workspace" / "home" / "index.html"
    assert index.exists()
    html = index.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    # Neutral: a model-facing comment, zero character flavor / user-visible text.
    assert "your homepage" in html
    assert "<title></title>" in html


def test_wake_website_module_choice_persisted_in_config():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path(), "website": "on"})
    cfg = json.loads(S.load_session(entry["name"]).config_path.read_text(encoding="utf-8"))
    assert cfg["website_override"] == "on"
    # Absent param → no override (chain stays card > off).
    plain = result("session.wake", {"card": luna_card_path()})
    plain_cfg = json.loads(S.load_session(plain["name"]).config_path.read_text(encoding="utf-8"))
    assert plain_cfg.get("website_override", "") == ""


def test_wake_invalid_website_is_clean_rpc_error():
    set_defaults()
    before = {m.name for m in S.list_sessions()}
    err = rpc_error("session.wake", {"card": luna_card_path(), "website": "sometimes"})
    assert err["code"] == -32602
    assert "website" in err["message"] and "on|off" in err["message"]
    assert {m.name for m in S.list_sessions()} == before


def test_set_modules_edits_config_for_next_start():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    name = entry["name"]
    r = result("session.set_modules", {"name": name, "website": True, "force_roleplay": True})
    assert r["website"] is True and r["force_roleplay"] is True and r["applies"] == "next_start"
    cfg = json.loads(S.load_session(name).config_path.read_text(encoding="utf-8"))
    assert cfg["website_override"] == "on"
    assert cfg["embodiment_override"] == "actor"
    # Turning website on ensured the scaffold exists.
    assert (S.load_session(name).sandbox_dir / "workspace" / "home" / "index.html").exists()
    # A second call changing only one module leaves the other intact.
    result("session.set_modules", {"name": name, "website": False})
    cfg2 = json.loads(S.load_session(name).config_path.read_text(encoding="utf-8"))
    assert cfg2["website_override"] == "off"
    assert cfg2["embodiment_override"] == "actor"  # untouched


def test_set_isolation_switches_config_for_next_start():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    name = entry["name"]
    assert entry["isolation"] == "sandbox"  # wakes sandboxed by default
    r = result("chara.set_isolation", {"name": name, "isolation": "admin"})
    assert r["isolation"] == "admin" and r["applies"] == "next_start"
    cfg = json.loads(S.load_session(name).config_path.read_text(encoding="utf-8"))
    assert cfg["isolation"] == "admin"
    # session.json is the JAIL AUTHORITY (meta.env() → LUNAMOTH_PY_BACKEND). Writing
    # only config.json left the toggle a no-op on the next child start.
    meta = S.load_session(name)
    assert meta.isolation == "admin"
    assert meta.env()["LUNAMOTH_PY_BACKEND"] == "admin"  # admin = unconfined backend
    # switch back — tightening, same path
    result("chara.set_isolation", {"name": name, "isolation": "sandbox"})
    cfg2 = json.loads(S.load_session(name).config_path.read_text(encoding="utf-8"))
    assert cfg2["isolation"] == "sandbox"
    meta2 = S.load_session(name)
    assert meta2.isolation == "sandbox"
    assert meta2.env()["LUNAMOTH_PY_BACKEND"] == "sandbox"  # sandbox = the jail backend


def test_set_isolation_rejects_unknown_value():
    set_defaults()
    name = result("session.wake", {"card": luna_card_path()})["name"]
    err = rpc_error("chara.set_isolation", {"name": name, "isolation": "rootkit"})
    assert err["code"] == -32602
    # unchanged — still the sandbox default
    assert S.load_session(name).isolation == "sandbox"


def test_wake_invalid_embodiment_is_clean_rpc_error_and_creates_nothing():
    set_defaults()
    before = {m.name for m in S.list_sessions()}
    err = rpc_error("session.wake", {"card": luna_card_path(), "embodiment": "puppet"})
    assert err["code"] == -32602
    assert "embodiment" in err["message"] and "literal|actor" in err["message"]
    assert {m.name for m in S.list_sessions()} == before  # validated before any disk writes


def test_wake_twice_gets_distinct_names_and_freezes_deck_card():
    set_defaults()
    a = result("session.wake", {"card": luna_card_path()})
    b = result("session.wake", {"card": luna_card_path()})
    assert a["name"] != b["name"]
    cards = result("cards.list")
    luna = next(c for c in cards if c["path"] == luna_card_path())
    assert luna["frozen"] is True
    assert set(luna["used_by"]) == {a["name"], b["name"]}


# ---- delete / export -----------------------------------------------------------

def test_delete_requires_exact_confirmation():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    err = rpc_error("session.delete", {"name": entry["name"], "confirm": "nope"})
    assert err["code"] == -32034
    result("session.delete", {"name": entry["name"], "confirm": entry["name"]})
    assert S.load_session(entry["name"]) is None


def test_export_zips_the_whole_session(tmp_path, monkeypatch):
    set_defaults()
    monkeypatch.setattr(H.Path, "home", classmethod(lambda cls: tmp_path))
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    (meta.sandbox_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (meta.sandbox_dir / "workspace" / "art.txt").write_text("aurora", encoding="utf-8")
    r = result("session.export", {"name": entry["name"]})
    assert r["path"].endswith(".zip")
    import zipfile

    names = zipfile.ZipFile(r["path"]).namelist()
    assert any(n.endswith("workspace/art.txt") for n in names)
    assert any(n.endswith("card.json") for n in names)


def _seed_transcript(meta):
    """Write a tiny transcript.db (chat + a struct tool call) without importing core/."""
    db = meta.sandbox_dir / "transcript.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, epoch INTEGER NOT NULL DEFAULT 0,"
        " role TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'chat', ts REAL NOT NULL);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    conn.execute("INSERT INTO messages(epoch,role,content,kind,ts) VALUES(0,'user','run it','chat',1.0)")
    call = json.dumps({"role": "assistant", "content": "", "reasoning_content": "thinking",
                       "tool_calls": [{"id": "c1", "type": "function",
                                       "function": {"name": "terminal", "arguments": "{}"}}]})
    conn.execute("INSERT INTO messages(epoch,role,content,kind,ts) VALUES(0,'assistant',?,'struct',2.0)", (call,))
    conn.commit()
    conn.close()


def test_export_includes_conversation_and_requests_jsonl(tmp_path, monkeypatch):
    set_defaults()
    monkeypatch.setattr(H.Path, "home", classmethod(lambda cls: tmp_path))
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    _seed_transcript(meta)
    (meta.sandbox_dir / "logs").mkdir(parents=True, exist_ok=True)
    (meta.sandbox_dir / "logs" / "requests.jsonl").write_text(
        json.dumps({"kind": "send", "model": "m", "system": ["sys"], "messages": [], "tools": []}) + "\n",
        encoding="utf-8")
    r = result("session.export", {"name": entry["name"]})

    # Standalone files next to the zip.
    assert r["conversation"].endswith("-conversation.jsonl")
    assert r["requests"].endswith("-requests.jsonl")
    conv_lines = [json.loads(ln) for ln in Path(r["conversation"]).read_text(encoding="utf-8").splitlines()]
    assert conv_lines[0]["content"] == "run it"
    assert conv_lines[1]["tool_calls"][0]["function"]["name"] == "terminal"
    assert conv_lines[1]["reasoning_content"] == "thinking"

    # And inside the zip too.
    import zipfile

    names = zipfile.ZipFile(r["path"]).namelist()
    assert any(n.endswith("-conversation.jsonl") for n in names)
    assert any(n.endswith("-requests.jsonl") for n in names)


def test_export_without_requests_log_omits_requests_key(tmp_path, monkeypatch):
    set_defaults()
    monkeypatch.setattr(H.Path, "home", classmethod(lambda cls: tmp_path))
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    _seed_transcript(meta)  # no requests.jsonl written
    r = result("session.export", {"name": entry["name"]})
    assert "requests" not in r
    assert "conversation" in r


# ---- cards: drafts, save, delete ------------------------------------------------

def test_card_from_draft_roundtrip():
    draft = {
        "name": "白枢", "appearance": "修复师。", "personality": "温和而固执。",
        "scenario": "长夜图书馆。", "first_mes": "轻一点关门。",
        "alternate_greetings": ["你来了。"],
        "world": [{"key": "长夜图书馆", "desc": "只在日落后开门。", "constant": True}],
        "relationship": "你是少数能进工作间的访客。",
        "goals": ["补完《长夜目录》"], "rules": "",
    }
    r = result("card.from_draft", {"draft": draft, "origin": "深夜图书馆修书人", "as_draft": True})
    card = json.loads((H.user_cards_dir() / "白枢.json").read_text(encoding="utf-8")) \
        if (H.user_cards_dir() / "白枢.json").exists() else json.loads(open(r["path"], encoding="utf-8").read())
    data = card["data"]
    assert data["name"] == "白枢"
    assert data["first_mes"] == "轻一点关门。"
    assert data["character_book"]["entries"][0]["keys"] == ["长夜图书馆"]
    # The card field is a boolean; an un-forced draft omits it entirely.
    assert "force_roleplay" not in data["extensions"]["lunamoth"]
    assert "embodiment" not in data["extensions"]["lunamoth"]
    assert "toolpack" not in data["extensions"]["lunamoth"]
    assert "tempo" not in data["extensions"]["lunamoth"]
    assert data["extensions"]["lunamoth"]["draft"] is True
    assert data["extensions"]["lunamoth"]["origin"] == "深夜图书馆修书人"
    listed = result("cards.list")
    mine = next(c for c in listed if c["name"] == "白枢")
    assert mine["draft"] is True and mine["builtin"] is False


def test_cards_draft_happy_path_uses_default_model(monkeypatch):
    set_defaults()
    seen = []
    mock_completion(monkeypatch, json.dumps(draft_payload()), seen)
    r = result("cards.draft", {"inspiration": "an orbital lantern keeper"})
    assert r["name"] == "Aster"
    assert r["user_name"] == "visitor"   # who "you" are in the world rides the draft
    assert r["personality"] == "Gentle, patient, quietly curious."
    assert r["scenario"].startswith("Dusk in the orbital garden")
    assert r["world_entries"][0]["keys"] == ["Orbital Garden"]
    assert r["polaris"] == "Map the whole mirror-season drift and welcome every careful visitor the dark sends"
    assert r["theme_color"] == "#7C5CFF"
    assert "avatar_svg" not in r          # the draft no longer auto-generates an avatar
    assert "embodiment" not in r          # the card field is force_roleplay (omitted when unset)
    payload = seen[0]["payload"]
    assert payload["model"] == "test/model"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.75
    assert seen[0]["api_key"] == "sk-test"


def test_cards_draft_tolerates_odd_world_and_goal_counts(monkeypatch):
    # The old strict "world_entries must contain 2-4" rejected valid drafts; now odd
    # counts (1, 5, or 0) and missing optional fields are tolerated, not errors.
    set_defaults()
    payload = draft_payload()
    payload["world_entries"] = [{"keys": ["only"], "content": "one entry", "constant": False}]  # just 1
    payload["polaris"] = ""                                                                       # empty is fine
    del payload["personality"]                                                                    # missing optional
    mock_completion(monkeypatch, json.dumps(payload))
    r = result("cards.draft", {"inspiration": "a lone keeper"})
    assert r["name"] == "Aster"
    assert len(r["world_entries"]) == 1
    assert r["polaris"] == ""
    assert r["personality"] == ""   # missing optional defaults to empty, no error


def test_cards_draft_invalid_json_is_clear_error_and_no_save(monkeypatch):
    set_defaults()
    mock_completion(monkeypatch, "not json")
    err = rpc_error("cards.draft", {"inspiration": "keep this exact text"})
    assert err["code"] == -32050
    assert "strict JSON" in err["message"]


def test_card_rewrite_field_with_instruction(monkeypatch):
    set_defaults()
    seen = []
    mock_completion(monkeypatch, "  A warmer, punchier tagline.  ", seen)
    r = result("card.rewrite_field", {
        "field": "tagline", "value": "old line",
        "instruction": "make it warmer", "context": "Name: Aster",
    })
    assert r == {"field": "tagline", "text": "A warmer, punchier tagline."}
    user_msg = seen[0]["payload"]["messages"][1]["content"]
    assert "make it warmer" in user_msg        # the instruction is forwarded
    assert "old line" in user_msg              # the current value is forwarded
    assert "Aster" in user_msg                 # context for consistency


def test_card_rewrite_field_empty_instruction_is_free_rephrase(monkeypatch):
    set_defaults()
    seen = []
    mock_completion(monkeypatch, "```\nfreshly rephrased\n```", seen)  # fence stripped
    r = result("card.rewrite_field", {"field": "description", "value": "v"})
    assert r["text"] == "freshly rephrased"
    assert "Rephrase it freely" in seen[0]["payload"]["messages"][1]["content"]


def test_card_rewrite_field_empty_model_output_errors(monkeypatch):
    set_defaults()
    mock_completion(monkeypatch, "   ")
    err = rpc_error("card.rewrite_field", {"field": "tagline", "value": "x"})
    assert err["code"] == -32050
    assert err["data"]["kind"] == "rewrite"


def test_cards_draft_rejects_parallel_schema(monkeypatch):
    set_defaults()
    bad = draft_payload()
    bad["extra"] = "nope"
    mock_completion(monkeypatch, json.dumps(bad))
    err = rpc_error("cards.draft", {"inspiration": "extra field"})
    assert err["code"] == -32050
    assert err["data"]["kind"] == "draft_schema"
    assert "unexpected: extra" in err["data"]["detail"]


def test_card_save_roundtrips_new_lunamoth_extension_fields():
    card = H.draft_to_card({
        **draft_payload(),
        "force_roleplay": True,
    }, origin_text="orbital lantern keeper")
    r = result("card.save", {"data": card})
    raw = result("card.read", {"path": r["path"]})["raw"]
    ext = raw["data"]["extensions"]["lunamoth"]
    assert "avatar_svg" not in ext        # avatar is a separate sidecar, not drafted inline
    assert ext["user_name"] == "visitor"  # who "you" are rides the drafted card
    # Presentation theme is now the dual {primary, secondary}; legacy single
    # theme_color folds into primary on save. A secondary is derived when not supplied,
    # so every drafted card has the two-color gradient (and it's a DISTINCT accent).
    assert ext["theme"]["primary"] == "#7C5CFF"
    sec = ext["theme"]["secondary"]
    assert sec.startswith("#") and len(sec) == 7 and sec != "#7C5CFF"
    assert "theme_color" not in ext
    assert ext["force_roleplay"] is True
    assert "embodiment" not in ext        # the legacy string is gone; the field is a bool
    assert "tempo" not in ext
    assert ext["tagline"] == "A gentle keeper of orbital lanterns"
    assert ext["polaris"] == "Map the whole mirror-season drift and welcome every careful visitor the dark sends"
    assert raw["data"]["character_book"]["entries"][0]["keys"] == ["Orbital Garden"]
    listed = result("cards.list")
    mine = next(c for c in listed if c["path"] == r["path"])
    assert mine["avatar_svg"] == ""       # no inline avatar (sidecar-only now)
    assert mine["theme_color"] == "#7C5CFF"
    assert mine["tagline"] == "A gentle keeper of orbital lanterns"


def test_draft_theme_always_has_valid_primary_and_secondary():
    """A drafted card ALWAYS carries a valid {primary, secondary}: the primary
    falls back to the deck color when the model omits/garbles theme_color, and
    the secondary is derived. A primary-less theme (which crashed the card view)
    can never be generated."""
    from lunamoth.server.hub.card_draft import _DEFAULT_THEME_PRIMARY
    # No theme_color at all → fallback primary + derived distinct secondary.
    no_theme = {k: v for k, v in draft_payload().items() if k != "theme_color"}
    t1 = H.draft_to_card(no_theme)["data"]["extensions"]["lunamoth"]["theme"]
    assert t1["primary"] == _DEFAULT_THEME_PRIMARY
    assert t1["secondary"].startswith("#") and t1["secondary"] != t1["primary"]
    # A garbage primary but a VALID secondary must NOT yield a secondary-only theme.
    t2 = H.draft_to_card({**no_theme, "theme_color": "not-a-color", "theme_color_2": "#445566"})[
        "data"]["extensions"]["lunamoth"]["theme"]
    assert t2["primary"] == _DEFAULT_THEME_PRIMARY
    assert t2["secondary"] == "#445566"


def test_card_studio_actor_embodiment_feeds_prompt_bridge(tmp_path, monkeypatch):
    """Studio-saved `extensions.lunamoth.force_roleplay=true` is read by the prompt machine."""
    card = H.draft_to_card({
        **draft_payload(),
        "name": "BridgeCard",
        "description": "BridgeCard keeps a quiet stage ledger.",
        "force_roleplay": True,
    }, origin_text="stage ledger keeper")
    r = result("card.save", {"data": card})

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core import agent as agent_mod
    from lunamoth.tools import skills as skills_mod

    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(agent_mod, "SANDBOX_ROOT", sandbox)
    monkeypatch.setattr(skills_mod, "SANDBOX_ROOT", sandbox)

    from lunamoth.core.agent import LunaMothAgent
    from lunamoth.session.settings import Settings

    agent = LunaMothAgent(Settings(provider="mock", character_path=r["path"], toolpack="sandbox"))
    agent.transcript.reset()
    stable = "\n\n".join(agent._stable_prefix())
    stable_words = " ".join(stable.split())

    assert Path(r["path"]).read_text(encoding="utf-8").find('\"force_roleplay\": true') >= 0
    assert agent.effective_embodiment() == "actor"
    assert "You are giving BridgeCard life" in stable
    assert "backstage of this embodiment" in stable_words
    assert stable.index("You are giving BridgeCard life") < stable.index("must be real")


def test_builtin_cards_cannot_be_deleted():
    err = rpc_error("card.delete", {"path": luna_card_path()})
    assert err["code"] == -32031


def test_referenced_card_cannot_be_deleted():
    set_defaults()
    draft = {"name": "T", "appearance": "x", "personality": "", "scenario": "",
             "first_mes": "hi", "alternate_greetings": [], "world": [],
             "relationship": "", "goals": [], "rules": "", "toolpack_hint": ""}
    r = result("card.from_draft", {"draft": draft})
    result("session.wake", {"card": r["path"]})
    err = rpc_error("card.delete", {"path": r["path"]})
    assert err["code"] == -32032


# ---- world import: card.merge_world + upload recognition --------------------------

def _world_book(entries):
    return {"name": "imported-world", "entries": entries}


def _saved_user_card():
    draft = {"name": "Holder", "appearance": "x", "personality": "", "scenario": "",
             "first_mes": "hi", "alternate_greetings": [], "world": [],
             "relationship": "", "goals": [], "rules": "", "toolpack_hint": ""}
    return result("card.from_draft", {"draft": draft})["path"]


def test_card_merge_world_appends_and_dedupes():
    path = _saved_user_card()
    world = _world_book([
        {"keys": ["alpha"], "content": "ALPHA LORE", "constant": True, "insertion_order": 1},
        {"key": ["beta"], "content": "BETA LORE", "order": 2},  # standalone-world field names
    ])
    r = result("card.merge_world", {"card_path": path, "world": world})
    assert r["added"] == 2 and r["entries"] == 2

    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    book = saved["data"]["character_book"]
    assert book["name"] == "imported-world"
    assert [(e["keys"], e["content"]) for e in book["entries"]] == [
        (["alpha"], "ALPHA LORE"), (["beta"], "BETA LORE"),
    ]
    assert book["entries"][0]["constant"] is True
    assert book["entries"][1]["insertion_order"] == 2

    # Merging the same book again: identical keys+content are skipped.
    again = result("card.merge_world", {"card_path": path, "world": world})
    assert again["added"] == 0 and again["entries"] == 2

    # The merged book round-trips through the normal card loader.
    card = H.CharacterCard.load(path)
    assert card.character_book is not None
    assert [e.content for e in card.character_book.entries] == ["ALPHA LORE", "BETA LORE"]


def test_card_merge_world_accepts_an_uploaded_world_path():
    path = _saved_user_card()
    up = H.store_upload("lore.json", json.dumps(_world_book([
        {"keys": ["gamma"], "content": "GAMMA LORE"},
    ])).encode("utf-8"))
    assert up["kind"] == "world"
    r = result("card.merge_world", {"card_path": path, "world": up["path"]})
    assert r["added"] == 1


def test_card_merge_world_refuses_outside_deck_and_bad_input():
    err = rpc_error("card.merge_world", {"card_path": luna_card_path(),
                                         "world": _world_book([{"keys": ["x"], "content": "y"}])})
    assert err["code"] == -32031  # builtin cards are not writable
    path = _saved_user_card()
    err = rpc_error("card.merge_world", {"card_path": path, "world": {"no": "entries"}})
    assert err["code"] == -32602


def test_upload_recognizes_world_books_and_cards():
    world = H.store_upload("w.json", json.dumps(_world_book([{"keys": ["k"], "content": "c"}])).encode("utf-8"))
    assert world["kind"] == "world"
    assert Path(world["path"]).parent == H.user_worlds_dir()

    card = H.store_upload("c.json", json.dumps({"data": {"name": "C", "description": "d"}}).encode("utf-8"))
    assert card["kind"] == "card"
    assert Path(card["path"]).parent == H.user_cards_dir()

    binary = H.store_upload("c.png", b"\x89PNG fake")
    assert binary["kind"] == "card"


# ---- works & extras --------------------------------------------------------------

def test_works_list_reads_the_works_shelf():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    # The shelf is workspace/works/ — only what the chara puts there is surfaced.
    shelf = meta.sandbox_dir / "workspace" / "works" / "gallery"
    shelf.mkdir(parents=True)
    (shelf / "aurora.html").write_text("<html>", encoding="utf-8")
    # A private workspace file (NOT under works/) must stay private.
    (meta.sandbox_dir / "workspace" / "scratch.txt").write_text("notes", encoding="utf-8")
    # A skip-dir (logs/) that happens to land under works/ is still skipped.
    (shelf / "logs").mkdir()
    (shelf / "logs" / "noise.log").write_text("x", encoding="utf-8")
    works = result("works.list", {"name": entry["name"]})
    names = [w["name"] for w in works]
    assert "aurora.html" in names
    assert "scratch.txt" not in names  # private workspace, not the shelf
    assert "noise.log" not in names  # logs are diagnostics, not works
    assert works[0]["kind"] == "web"


def test_works_list_excludes_private_workspace_and_assets():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    # Legacy files/ tree, a private workspace file, and the read-only assets
    # sibling are all NOT works — none should be listed.
    (meta.sandbox_dir / "files").mkdir(parents=True, exist_ok=True)
    (meta.sandbox_dir / "files" / "ghost.txt").write_text("residue", encoding="utf-8")
    (meta.sandbox_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (meta.sandbox_dir / "workspace" / "draft.md").write_text("private", encoding="utf-8")
    (meta.sandbox_dir / "assets").mkdir(parents=True, exist_ok=True)
    (meta.sandbox_dir / "assets" / "sprite.png").write_bytes(b"\x89PNG")
    names = [w["name"] for w in result("works.list", {"name": entry["name"]})]
    assert "ghost.txt" not in names
    assert "draft.md" not in names
    assert "sprite.png" not in names


def test_works_read_refuses_legacy_files_path():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    (meta.sandbox_dir / "files").mkdir(parents=True, exist_ok=True)
    (meta.sandbox_dir / "files" / "ghost.txt").write_text("residue", encoding="utf-8")
    err = rpc_error("works.read", {"name": entry["name"], "rel": "files/ghost.txt"})
    assert err["code"] == -32031


def test_works_read_serves_workspace_file():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    ws = meta.sandbox_dir / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "poem.md").write_text("# aurora", encoding="utf-8")
    got = result("works.read", {"name": entry["name"], "rel": "workspace/poem.md"})
    assert got["kind"] == "text"
    assert got["content"] == "# aurora"


def test_open_path_refuses_outside_home():
    err = rpc_error("open.path", {"path": "/etc/hosts"})
    assert err["code"] in (-32040, -32041)


# ---- error classification ---------------------------------------------------------

def test_http_error_classification():
    assert H._classify_http_error(401, "")["kind"] == "auth"
    assert H._classify_http_error(402, "")["kind"] == "credit"
    assert H._classify_http_error(500, "Insufficient credits")["kind"] == "credit"
    assert H._classify_http_error(429, "")["kind"] == "ratelimit"
    assert H._classify_http_error(404, "")["kind"] == "model"


def test_board_error_kind_classifies_provider_auth_errors():
    assert H.board_error_kind("2026 ERROR llm: permanent HTTP error: HTTP 401 User not found") == "auth"
    assert H.board_error_kind("HTTP 403 forbidden") == "auth"
    assert H.board_error_kind("HTTP 404 model not found") == "model"
    assert H.board_error_kind("connection failed: timeout") == "network"


def test_session_entry_includes_auth_error_kind():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    log = meta.sandbox_dir / "logs" / "errors.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("2026-06-12 ERROR [x] lunamoth.terminal: permanent model error: HTTP 401 User not found\n",
                   encoding="utf-8")
    row = result("sessions.list")[0]
    assert row["error_kind"] == "auth"
    assert "HTTP 401" in row["error"]


def test_board_state_extracts_recent_speak_tool_calls_from_transcript():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    db = meta.sandbox_dir / "transcript.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, epoch INTEGER NOT NULL DEFAULT 0, "
            "role TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'chat', ts REAL NOT NULL)"
        )
        rows = [
            (
                "assistant",
                json.dumps({
                    "role": "assistant",
                    "tool_calls": [{
                        "function": {"name": "speak", "arguments": json.dumps({"text": "old hello"})},
                    }],
                }),
                "struct",
                99.0,
            ),
            (
                "assistant",
                json.dumps({
                    "role": "assistant",
                    "tool_calls": [{
                        "function": {"name": "terminal", "arguments": json.dumps({"cmd": "echo ignored"})},
                    }],
                }),
                "struct",
                100.0,
            ),
            (
                "assistant",
                json.dumps({
                    "role": "assistant",
                    "tool_calls": [{
                        "function": {"name": "speak", "arguments": "{"},
                    }],
                }),
                "struct",
                101.0,
            ),
            (
                "assistant",
                json.dumps({
                    "role": "assistant",
                    "tool_calls": [{
                        "function": {"name": "speak", "arguments": json.dumps({"text": "first hello"})},
                    }],
                }),
                "struct",
                102.0,
            ),
            (
                "assistant",
                json.dumps({
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "memory", "arguments": json.dumps({"text": "ignored"})}},
                        {"function": {"name": "speak", "arguments": json.dumps({"text": "second hello"})}},
                    ],
                }),
                "struct",
                103.0,
            ),
            (
                "assistant",
                json.dumps({
                    "role": "assistant",
                    "tool_calls": [{
                        "function": {"name": "speak", "arguments": json.dumps({"text": "newest hello"})},
                    }],
                }),
                "struct",
                104.0,
            ),
        ]
        conn.executemany("INSERT INTO messages(role, content, kind, ts) VALUES(?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()

    row = result("sessions.list")[0]

    assert row["speaks"] == [
        {"text": "newest hello", "ts": 104.0},
        {"text": "second hello", "ts": 103.0},
        {"text": "first hello", "ts": 102.0},
    ]


def test_unknown_method_is_a_clean_rpc_error():
    err = rpc_error("nope.nothing")
    assert err["code"] == -32601
